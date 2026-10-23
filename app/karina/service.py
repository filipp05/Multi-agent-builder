"""Standalone user path and integration helpers; upstream stages remain supplied documents."""
from copy import deepcopy
import json
from uuid import uuid4

from jsonschema import ValidationError

from contracts.handoff import IDENTITY, call_stage, make_context
from contracts.validate import validate_bundle
from app.karina import agents
from app.karina.audit import load_rules
from app.karina.common import InputError, ROOT, require
from app.karina.schedule import build_schedule
from app.karina.stages import audit_handler, schedule_handler

UPSTREAM = ('brief', 'design_options', 'design_selection', 'work_plan', 'procurement', 'estimate')
EXAMPLE_LABEL = 'Учебный пример: требования, концепции, работы, закупки и смета подготовлены заранее. Эти этапы здесь не генерируются моделью. Цены и нормы вымышлены.'


class StageFailed(InputError):
    def __init__(self, result):
        super().__init__(result['error']['message'], code=result['error']['code'])
        self.result = result


def example_bundle():
    return json.loads((ROOT/'contracts/examples/completed.json').read_text())


def snapshot(bundle):
    require(isinstance(bundle, dict), 'Нужен комплект документов', 'bundle')
    for key in (*IDENTITY, 'artifacts', 'sources', 'offers'):
        require(key in bundle, f'В комплекте нет поля {key}', f'bundle.{key}')
    data = deepcopy(bundle)
    source = load_rules()['source']
    existing = next((s for s in data['sources'] if s['id'] == source['id']), None)
    require(existing is None or existing == source, 'Источник контрольных правил описан по-разному', 'sources')
    if existing is None:
        data['sources'].append(source)
    return data


def audit_context(data):
    """Do not reject stale/invalid payloads before the independent auditor can report them."""
    docs = [a for a in data['artifacts'] if a['kind'] != 'audit']
    expected = set(UPSTREAM) | {'schedule'}
    require({a['kind'] for a in docs} == expected and len(docs) == len(expected),
            'Для аудита нужны семь разных документов до audit', 'artifacts')
    require(len({a['artifact_id'] for a in docs}) == len(docs), 'Повтор идентификатора документа', 'artifacts')
    return {**{k: data[k] for k in IDENTITY}, 'stage': 'audit',
            'input_artifact_ids': [a['artifact_id'] for a in docs], 'input_artifacts': docs,
            'referenced_artifacts': [], 'sources': data['sources'], 'offers': data['offers'],
            'user_request': None, 'selected_concept_id': None}


def accepted(result, doc):
    if result['status'] != 'ok':
        raise StageFailed(result)
    return doc


def assemble(data, audit):
    bundle = {**data, 'artifacts': [a for a in data['artifacts'] if a['kind'] != 'audit']+[audit]}
    kinds = {a['kind']: a for a in bundle['artifacts']}
    ready = (audit['payload']['status'] == 'passed' and
             kinds['estimate']['payload'].get('budget_status') == 'satisfied' and
             kinds['schedule']['payload'].get('deadline_status') == 'satisfied')
    bundle['status'] = 'completed' if ready else 'constraints_unresolved'
    if ready:
        # Additional integration gate; it does not replace the independent checker.
        try:
            validate_bundle(bundle)
        except (ValueError, ValidationError):
            audit['payload']['status'] = 'failed'
            audit['payload']['findings'].append({
                'id': f'finding-{len(audit["payload"]["findings"])+1}', 'severity': 'blocking',
                'code': 'INTEGRATION_CHECK', 'message': 'Общий комплект не прошёл дополнительную проверку согласованности. Исправьте входные документы.',
                'artifact_ids': [kinds['work_plan']['artifact_id']], 'item_ids': [], 'owner': 'karina', 'source_ids': []})
            bundle['status'] = 'constraints_unresolved'
    return bundle


async def audit_existing(bundle, *, model=None, new_id=lambda: str(uuid4())):
    data = snapshot(bundle)
    context = audit_context(data)
    # Validate and perform deterministic checks before spending a model call.
    initial, initial_doc = call_stage(audit_handler(new_id=new_id), context)
    accepted(initial, initial_doc)
    semantic, log = None, []
    if model is not None:
        # Malformed records cannot be meaningfully reviewed; preserve the concrete code findings.
        if not any(f['code'] in ('DOCUMENT_SCHEMA', 'REGISTRY_SCHEMA') for f in initial_doc['payload']['findings']):
            semantic, metadata = await agents.review_bundle(model, context, load_rules())
            log.append({'agent': 'контролёр', **metadata})
    if semantic is None:
        result, doc = initial, initial_doc
    else:
        result, doc = call_stage(audit_handler(semantic_findings=semantic['findings'], new_id=new_id), context)
        accepted(result, doc)
    assembled = assemble(data, doc)
    return {'mode': 'model' if semantic is not None else 'checks_only', 'audit': doc,
            'controller': semantic, 'model_log': log, 'results': [result],
            'bundle': assembled, 'view': present(assembled)}


async def run(bundle, *, model=None, wishes='', new_id=lambda: str(uuid4())):
    data = snapshot(bundle)
    upstream = [a for a in data['artifacts'] if a['kind'] in UPSTREAM]
    context = make_context(data, 'schedule', upstream, data['sources'], data['offers'])
    result, schedule = call_stage(schedule_handler(new_id=new_id), context)
    accepted(result, schedule)
    _, _, details = build_schedule(context)
    planner, log = None, []
    if model is not None:
        planner, metadata = await agents.explain_schedule(model, context, schedule, details, wishes)
        log.append({'agent': 'планировщик', **metadata})
    data['artifacts'] = upstream+[schedule]
    review = await audit_existing(data, model=model, new_id=new_id)
    return {**review, 'schedule': schedule, 'planner': planner, 'calculation_details': details,
            'model_log': log+review['model_log'], 'results': [result]+review['results']}


def present(bundle):
    docs = {a['kind']: a for a in bundle['artifacts']}
    brief, work, schedule, audit = (docs[k]['payload'] for k in ('brief', 'work_plan', 'schedule', 'audit'))
    tasks = {t['id']: t for t in work.get('tasks', [])}
    rows = [{**slot, 'title': tasks.get(slot['task_id'], {}).get('title', slot['task_id']),
             'predecessor_ids': tasks.get(slot['task_id'], {}).get('predecessor_ids', []),
             'labor_hours': tasks.get(slot['task_id'], {}).get('labor_hours'),
             'wait_after_hours': tasks.get(slot['task_id'], {}).get('wait_after_hours')}
            for slot in schedule.get('items', [])]
    return {'status': bundle['status'], 'audit_status': audit['status'], 'findings': audit['findings'],
            'finish_at': schedule.get('finish_at'), 'deadline_status': schedule.get('deadline_status'),
            'deadline': brief.get('deadline'), 'calendar': brief.get('calendar'), 'rows': rows,
            'assumptions': docs['schedule']['assumptions']+docs['audit']['assumptions']}
