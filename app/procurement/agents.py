"""Снабженец и сметчик: модель выбирает и объясняет, код фильтрует кандидатов, считает и проверяет ответ."""
from pathlib import Path

from app.brief.model import ModelError, ModelRequest
from app.procurement import calc
from app.procurement.stages import artifact_of, need_with_waste

PROMPTS = Path(__file__).parent/'prompts'

CHOICE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['choices'],
    'properties': {'choices': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['requirement_id', 'offer_id', 'reason'],
        'properties': {'requirement_id': {'type': 'string'}, 'offer_id': {'type': 'string'}, 'reason': {'type': 'string', 'minLength': 1}}}}},
}

EXPLAIN_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['summary', 'explanations'],
    'properties': {'summary': {'type': 'string', 'minLength': 1}, 'explanations': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['line_id', 'text'],
        'properties': {'line_id': {'type': 'string'}, 'text': {'type': 'string', 'minLength': 1}}}}},
}


def candidates(context, rates):
    """Только совместимые предложения из снимка; количество и деньги считает код."""
    work = artifact_of(context, 'work_plan')['payload']
    brief = artifact_of(context, 'brief')['payload']
    suppliers = {s['id']: s for s in rates['suppliers']}
    result = {}
    for req in work['requirements']:
        options = []
        for offer in context['offers']:
            if calc.mismatch(req, offer, brief['currency']) is None:
                bought = calc.purchase(req, offer)
                rate = suppliers.get(offer['supplier_id'])
                options.append({'offer_id': offer['id'], 'supplier_id': offer['supplier_id'],
                                'pack_quantity': offer['pack_quantity'], 'pack_count': bought['pack_count'],
                                'amount_minor': bought['amount_minor'], 'available_on': offer['available_on'],
                                'delivery_minor': rate['delivery_minor'] if rate else None})
        if not options:
            raise ValueError(f"Нет совместимого предложения для потребности «{req['id']}» в снимке каталога")
        result[req['id']] = options
    return result


async def choose_offers(model, context, rates):
    """Возвращает ({requirement_id: offer_id}, {requirement_id: reason}, metadata вызова)."""
    work = artifact_of(context, 'work_plan')['payload']
    brief = artifact_of(context, 'brief')['payload']
    options = candidates(context, rates)
    data = {'requirements': [{'id': r['id'], 'category': r['category'], 'need_with_waste': {'value': need_with_waste(r), 'unit': r['base_quantity']['unit']}}
                             for r in work['requirements']],
            'candidates': options, 'budget_minor': brief['budget_minor'], 'start_date': brief['start_date'],
            'hard_preferences': brief['hard_preferences'], 'soft_preferences': brief['soft_preferences']}
    reply = await model.generate(ModelRequest((PROMPTS/'procurement.txt').read_text(encoding='utf-8'), data, CHOICE_SCHEMA, 120))
    choices, reasons = {}, {}
    for item in reply.data['choices']:
        rid, oid = item['requirement_id'], item['offer_id']
        if rid not in options or rid in choices:
            raise ModelError('invalid_choice', {'requirement_id': rid, 'reason': 'неизвестная или повторная потребность'})
        if oid not in {x['offer_id'] for x in options[rid]}:
            raise ModelError('invalid_choice', {'requirement_id': rid, 'offer_id': oid, 'reason': 'артикул не из списка кандидатов'})
        choices[rid], reasons[rid] = oid, item['reason']
    if set(choices) != set(options):
        raise ModelError('invalid_choice', {'missing': sorted(set(options)-set(choices))})
    return choices, reasons, reply.metadata


async def explain_estimate(model, context, procurement, estimate):
    """Пояснения к готовым строкам. Каждая строка объяснена ровно один раз; суммы модель не меняет."""
    brief = artifact_of(context, 'brief')['payload']
    work = artifact_of(context, 'work_plan')['payload']
    sources = {s['id']: s for s in context['sources']}
    titles = {t['id']: t['title'] for t in work['tasks']}
    buys = {p['id']: p for p in procurement['payload']['purchases']}
    est = estimate['payload']
    lines = [{'id': line['id'], 'kind': line['kind'], 'reference_id': line['reference_id'], 'amount_minor': line['amount_minor'],
              'about': titles.get(line['reference_id']) or (buys[line['reference_id']]['offer_id'] if line['reference_id'] in buys else line['reference_id']),
              'sources': [{'id': x, 'kind': sources[x]['kind'], 'reference': sources[x]['reference']} for x in line['source_ids']]}
             for line in est['lines']]
    data = {'lines': lines, 'base_minor': est['base_minor'], 'reserve_percent': est['reserve_percent'], 'reserve_minor': est['reserve_minor'],
            'total_minor': est['total_minor'], 'budget_minor': brief['budget_minor'], 'budget_status': est['budget_status'],
            'assumptions': estimate['assumptions']}
    reply = await model.generate(ModelRequest((PROMPTS/'estimate.txt').read_text(encoding='utf-8'), data, EXPLAIN_SCHEMA, 120))
    texts = {}
    for item in reply.data['explanations']:
        if item['line_id'] in texts or item['line_id'] not in {x['id'] for x in lines}:
            raise ModelError('invalid_explanation', {'line_id': item['line_id']})
        texts[item['line_id']] = item['text']
    if set(texts) != {x['id'] for x in lines}:
        raise ModelError('invalid_explanation', {'missing': sorted({x['id'] for x in lines}-set(texts))})
    return {'summary': reply.data['summary'], 'lines': texts}, reply.metadata
