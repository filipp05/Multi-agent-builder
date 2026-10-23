"""Путь «план работ → корзина → смета» на общем договоре 0.1.0. Чужие этапы берутся из присланного комплекта."""
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker

from contracts.handoff import IDENTITY, call_stage, make_context
from contracts.validate import ROOT as CONTRACTS, index, validate_bundle
from app.procurement import agents, calc, check
from app.procurement.stages import artifact_of, estimate_handler, load, need_with_waste, procurement_handler

UPSTREAM = ('brief', 'design_options', 'design_selection', 'work_plan')
EXAMPLE = CONTRACTS/'examples/completed.json'
EXAMPLE_LABEL = ('Учебный пример contracts/examples/completed.json: бриф, концепции и план работ — готовые данные, '
                 'не результат модели. Каталог расширен снимком app/procurement/data/catalog.json.')


def schema_items(name):
    schema = json.loads((CONTRACTS/'schemas/plan.schema.json').read_text(encoding='utf-8'))
    return Draft202012Validator(schema['properties'][name]['items'], format_checker=FormatChecker())


def merge_sources(*groups):
    merged = {}
    for source in (s for g in groups for s in g):
        if source['id'] in merged and merged[source['id']] != source:
            raise ValueError(f"Источник «{source['id']}» описан по-разному")
        merged[source['id']] = source
    return list(merged.values())


def example_bundle():
    """Общий пример с расширенным учебным каталогом: три исходных предложения сохранены без изменений."""
    bundle = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    catalog = load('catalog.json')
    bundle['sources'] = merge_sources(bundle['sources'], catalog['sources'])
    bundle['offers'] = catalog['offers']
    return bundle


def example_choices():
    bundle = json.loads(EXAMPLE.read_text(encoding='utf-8'))
    proc = index(bundle['artifacts'], 'kind')['procurement']['payload']
    return {p['requirement_id']: p['offer_id'] for p in proc['purchases']}


def contexts_from(bundle, rates):
    """Проверить присланный снимок и построить контекст procurement. Тарифы Олега добавляются к источникам явно."""
    for key in ('artifacts', 'sources', 'offers', *IDENTITY):
        if key not in bundle:
            raise ValueError(f'В комплекте нет поля {key}')
    identity = {k: bundle[k] for k in IDENTITY}
    upstream = [a for a in bundle['artifacts'] if a.get('kind') in UPSTREAM]
    artifacts = schema_items('artifacts')
    for a in upstream:
        error = next(artifacts.iter_errors(a), None)
        if error:
            raise ValueError(f"Некорректный документ {a.get('kind')}: {error.message}")
    brief = next((a['payload'] for a in upstream if a['kind'] == 'brief'), None)
    if brief is None:
        raise ValueError('В комплекте нет брифа')
    offers = schema_items('offers')
    for offer in bundle['offers']:
        if isinstance(offer, dict) and offer.get('currency') != brief['currency']:
            raise ValueError(f"Валюта предложения «{offer.get('id')}» ({offer.get('currency')}) не совпадает с валютой бюджета ({brief['currency']})")
        error = next(offers.iter_errors(offer), None)
        if error:
            raise ValueError(f"Некорректное предложение «{offer.get('id') if isinstance(offer, dict) else offer}»: {error.message}")
    index(bundle['offers'])
    sources = schema_items('sources')
    for source in bundle['sources']:
        error = next(sources.iter_errors(source), None)
        if error:
            raise ValueError(f'Некорректный источник: {error.message}')
    all_sources = merge_sources(bundle['sources'], rates['sources'])
    return identity, upstream, all_sources, make_context(identity, 'procurement', upstream, all_sources, bundle['offers'])


class StageFailed(ValueError):
    def __init__(self, result):
        super().__init__(result['error']['message'])
        self.result = result


async def run(bundle, *, choices=None, model=None, rates=None, new_id=lambda: str(uuid4())):
    """choices=None — выбор делает модель; иначе явный выбор {requirement_id: offer_id} человека или примера."""
    rates = rates or load('rates.json')
    identity, upstream, sources, context = contexts_from(bundle, rates)
    log, reasons = [], {}
    if choices is None:
        if model is None:
            raise ValueError('Не задан ни выбор предложений, ни модель')
        choices, reasons, meta = await agents.choose_offers(model, context, rates)
        log.append({'agent': 'снабженец', **meta})
    result, proc = call_stage(procurement_handler(choices, rates, new_id), context)
    if result['status'] != 'ok':
        raise StageFailed(result)
    est_context = make_context(identity, 'estimate', upstream+[proc], sources, bundle['offers'])
    est_result, est = call_stage(estimate_handler(rates, new_id), est_context)
    if est_result['status'] != 'ok':
        raise StageFailed(est_result)
    explanations = None
    if reasons:
        explanations, meta = await agents.explain_estimate(model, est_context, proc, est)
        log.append({'agent': 'сметчик', **meta})
    return {'mode': 'model' if reasons else 'manual', 'choices': choices, 'reasons': reasons,
            'explanations': explanations, 'model_log': log, 'results': [result, est_result],
            'procurement': proc, 'estimate': est, 'sources': sources, 'view': present(est_context, proc, est, rates)}


def verify(bundle, procurement, estimate, rates=None):
    rates = rates or load('rates.json')
    _, _, _, context = contexts_from(bundle, rates)
    return check.verify(context, procurement, estimate, rates)


def assemble_bundle(bundle, procurement, estimate, sources):
    """Подставить свои документы в готовый комплект и перепривязать ссылки чужих документов. Для проверки договора."""
    data = deepcopy(bundle)
    old = index(data['artifacts'], 'kind')
    renamed = {old['procurement']['artifact_id']: procurement['artifact_id'], old['estimate']['artifact_id']: estimate['artifact_id']}
    artifacts = []
    for a in data['artifacts']:
        a = {'procurement': procurement, 'estimate': estimate}.get(a['kind'], a)
        artifacts.append({**a, 'input_artifact_ids': [renamed.get(x, x) for x in a['input_artifact_ids']]})
    data.update(artifacts=artifacts, sources=sources)
    validate_bundle(data)
    return data


def present(context, procurement, estimate, rates):
    """Данные для страницы: каждая строка со ссылками на потребность, предложение и источник."""
    work = artifact_of(context, 'work_plan')['payload']
    brief = artifact_of(context, 'brief')['payload']
    offers = index(context['offers'])
    sources = index(context['sources'])
    requirements = index(work['requirements'])
    tasks = index(work['tasks'])
    cite = lambda ids: [{'id': x, 'kind': sources[x]['kind'], 'captured_at': sources[x]['captured_at'], 'reference': sources[x]['reference']} for x in ids]
    purchases = []
    for p in procurement['payload']['purchases']:
        req, offer = requirements[p['requirement_id']], offers[p['offer_id']]
        purchases.append({**p, 'category': req['category'], 'supplier_id': offer['supplier_id'],
                          'base_quantity': req['base_quantity'], 'waste_percent': req['waste_percent'],
                          'need_with_waste': {'value': need_with_waste(req), 'unit': req['base_quantity']['unit']},
                          'pack_quantity': offer['pack_quantity'], 'available_on': offer['available_on'], 'sources': cite(offer['source_ids'])})
    deliveries = [{**d, 'sources': cite(d['source_ids'])} for d in procurement['payload']['deliveries']]
    labels = {'material': {p['id']: f"{p['offer_id']} × {p['pack_count']} уп." for p in purchases},
              'labor': {t: tasks[t]['title'] for t in tasks},
              'delivery': {d['id']: f"Доставка от {d['supplier_id']}" for d in deliveries}}
    est = estimate['payload']
    lines = [{**line, 'label': labels[line['kind']].get(line['reference_id'], line['reference_id']), 'sources': cite(line['source_ids'])} for line in est['lines']]
    subtotals = {}
    for kind in ('material', 'labor', 'delivery'):
        amounts = [line['amount_minor'] for line in est['lines'] if line['kind'] == kind]
        subtotals[kind] = None if None in amounts else sum(amounts)
    known = [line['amount_minor'] for line in est['lines'] if line['amount_minor'] is not None]
    paint_tasks = [tasks[t] for r in work['requirements'] if r['category'] == 'paint' for t in r['task_ids']]
    total, budget = est['total_minor'], brief['budget_minor']
    return {'currency': brief['currency'], 'purchases': purchases, 'deliveries': deliveries, 'lines': lines, 'subtotals': subtotals,
            'partial_known_minor': sum(known) if est['total_minor'] is None else None,
            'base_minor': est['base_minor'], 'reserve_percent': est['reserve_percent'], 'reserve_rule': rates['reserve']['version'],
            'reserve_minor': est['reserve_minor'], 'total_minor': total, 'budget_minor': budget, 'budget_status': est['budget_status'],
            'shortfall_minor': total-budget if total is not None and total > budget else (0 if total is not None else None),
            'room_check': {'wall_area_m2': calc.text(calc.wall_area(brief['room'])),
                           'paint_task_m2': [t['quantity']['value'] for t in paint_tasks]},
            'assumptions': procurement['assumptions']+[a for a in estimate['assumptions'] if a not in procurement['assumptions']]}
