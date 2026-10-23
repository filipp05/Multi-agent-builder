"""Coordinator-facing handlers. Both use the team's unchanged stage protocol."""
from decimal import InvalidOperation
from uuid import uuid4

from contracts.handoff import IDENTITY
from app.karina.audit import inspect, load_rules
from app.karina.common import InputError, artifact, validate_context
from app.karina.schedule import build_schedule


def _handler(kind, build, new_id):
    def handle(context):
        result = {**{k: context[k] for k in IDENTITY}, 'stage': kind, 'questions': []}
        try:
            payload, assumptions, sources = build(context)
        except (InputError, KeyError, TypeError, ValueError, InvalidOperation, OverflowError) as error:
            return {**result, 'status': 'failed', 'artifact_id': None,
                    'error': {'code': getattr(error, 'code', 'INVALID_INPUT'), 'message': str(error),
                              'retryable': False, 'field_paths': getattr(error, 'paths', ['input_artifacts'])}}, None
        doc = artifact(context, kind, payload, new_id(), assumptions, sources)
        return {**result, 'status': 'ok', 'artifact_id': doc['artifact_id'], 'error': None}, doc
    return handle


def schedule_handler(*, new_id=lambda: str(uuid4())):
    def build(context):
        payload, assumptions, _ = build_schedule(context)
        sources = {s for a in context['input_artifacts'] for s in a['source_ids']}
        return payload, assumptions, sources
    return _handler('schedule', build, new_id)


def audit_handler(*, rules=None, semantic_findings=None, new_id=lambda: str(uuid4())):
    rules = rules or load_rules()

    def build(context):
        validate_context(context, 'audit', inspect_artifacts=False)
        payload = inspect(context, rules)
        assumptions = ['Контрольные количества, статьи, упаковки, суммы и занятость бригады проверены отдельным алгоритмом Карины. Цены и нормы учебные.']
        if semantic_findings is None:
            if payload['status'] == 'passed':
                payload['status'] = 'incomplete'
            assumptions.append('Выполнена только программная проверка; смысловой LLM-контролёр не запускался. Общий успех не подтверждён.')
        else:
            from app.karina.agents import validate_findings
            from app.filipp.model import ModelError
            try:
                validate_findings(semantic_findings, context)
            except ModelError as error:
                raise InputError('Замечания контролёра имеют неверный формат или ссылки', 'semantic_findings') from error
            for finding in semantic_findings:
                payload['findings'].append({**finding, 'id': f'finding-{len(payload["findings"])+1}'})
            if any(f['severity'] == 'blocking' for f in payload['findings']):
                payload['status'] = 'failed'
            elif any(f['code'] == 'LLM_DATA_INCOMPLETE' for f in semantic_findings):
                payload['status'] = 'incomplete'
        sources = {rules['source']['id']} & {s['id'] for s in context['sources']}
        return payload, assumptions, sources
    return _handler('audit', build, new_id)
