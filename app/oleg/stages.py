"""Обработчики этапов procurement и estimate по общему интерфейсу handler(context) -> (result, artifact)."""
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.oleg import calc

IDENTITY = ('schema_version', 'project_id', 'run_id', 'revision')
DATA = Path(__file__).parent/'data'


def load(name):
    return json.loads((DATA/name).read_text(encoding='utf-8'))


class StageInputError(ValueError):
    def __init__(self, message, field_paths=()):
        super().__init__(message)
        self.field_paths = list(field_paths)


def check(condition, message, *field_paths):
    if not condition:
        raise StageInputError(message, field_paths)


def artifact_of(context, kind):
    found = [a for a in context['input_artifacts']+context['referenced_artifacts'] if a['kind'] == kind]
    check(len(found) == 1, f'Во входах этапа нет документа {kind}', kind)
    return found[0]


def known_sources(context, source_ids, what):
    registry = {s['id'] for s in context['sources']}
    missing = sorted(set(source_ids)-registry)
    check(not missing, f"{what}: источники {', '.join(missing)} отсутствуют в снимке источников", 'sources')


def stage(kind, build, new_id):
    """Общая обёртка: ошибка входа превращается в failed без документа, а не в исключение."""
    def handler(context):
        result = {**{k: context[k] for k in IDENTITY}, 'stage': kind, 'questions': []}
        try:
            payload, source_ids, assumptions = build(context)
        except StageInputError as e:
            return {**result, 'status': 'failed', 'artifact_id': None,
                    'error': {'code': 'INVALID_INPUT', 'message': str(e), 'retryable': False, 'field_paths': e.field_paths}}, None
        artifact = {**{k: context[k] for k in IDENTITY}, 'artifact_id': new_id(), 'kind': kind,
                    'input_artifact_ids': list(context['input_artifact_ids']), 'source_ids': sorted(source_ids),
                    'assumptions': assumptions, 'payload': payload}
        return {**result, 'status': 'ok', 'artifact_id': artifact['artifact_id'], 'error': None}, artifact
    return handler


def source_note(context, source_ids):
    registry = {s['id']: s for s in context['sources']}
    used = [registry[x] for x in sorted(source_ids)]
    notes = []
    if any(s['kind'] == 'synthetic' for s in used):
        dates = sorted({s['captured_at'][:10] for s in used if s['kind'] == 'synthetic'})
        notes.append(f"Цены и тарифы учебные (synthetic), снимки от {', '.join(dates)}: это не рыночная оценка и не проверка наличия.")
    return notes


def build_procurement(context, choices, rates):
    work = artifact_of(context, 'work_plan')['payload']
    brief = artifact_of(context, 'brief')['payload']
    offers = {o['id']: o for o in context['offers']}
    requirements = {r['id']: r for r in work['requirements']}
    for rid in sorted(set(choices)-set(requirements)):
        check(False, f'Выбор для неизвестной потребности «{rid}»', f'choices.{rid}')
    for rid in sorted(set(requirements)-set(choices)):
        check(False, f'Нет выбранного предложения для потребности «{rid}»', f'choices.{rid}')
    purchases, used = [], []
    for rid, requirement in requirements.items():
        offer = offers.get(choices[rid])
        check(offer is not None, f'Неизвестный артикул «{choices[rid]}»: в снимке каталога такого предложения нет', f'choices.{rid}')
        problem = calc.mismatch(requirement, offer, brief['currency'])
        check(problem is None, problem or '', f'choices.{rid}')
        known_sources(context, offer['source_ids'], f"Предложение «{offer['id']}»")
        purchases.append(calc.purchase(requirement, offer))
        used.append(offer)
    suppliers = {s['id']: s for s in rates['suppliers']}
    rate_sources = [s['id'] for s in rates['sources']]
    known_sources(context, rate_sources, 'Тарифы доставки')
    delivery = calc.deliveries(used, suppliers, rate_sources)
    source_ids = {x for o in used for x in o['source_ids']} | {x for d in delivery for x in d['source_ids']}
    assumptions = source_note(context, source_ids)
    for d in delivery:
        if d['amount_minor'] is None:
            assumptions.append(f"Стоимость доставки от {d['supplier_id']} неизвестна: смета не может считаться полной.")
        if d['available_on'] is None:
            assumptions.append(f"Дата поставки от {d['supplier_id']} неизвестна: расписание остаётся предварительным.")
    return {'purchases': purchases, 'deliveries': delivery}, source_ids, assumptions


def build_estimate(context, rates):
    work = artifact_of(context, 'work_plan')['payload']
    brief = artifact_of(context, 'brief')['payload']
    proc = artifact_of(context, 'procurement')['payload']
    offers = {o['id']: o for o in context['offers']}
    rule = rates['reserve']
    known_sources(context, rule['source_ids'], f"Правило резерва {rule['version']}")
    lines, assumptions = [], []
    for p in proc['purchases']:
        offer = offers.get(p['offer_id'])
        check(offer is not None, f"Покупка «{p['id']}» ссылается на неизвестный артикул «{p['offer_id']}»", f"procurement.purchases.{p['id']}")
        lines.append({'id': f"line-material-{p['id']}", 'kind': 'material', 'reference_id': p['id'],
                      'amount_minor': p['amount_minor'], 'source_ids': offer['source_ids']})
    rate_sources = [s['id'] for s in rates['sources']]
    for task in work['tasks']:
        rate, reason = calc.labor_rate(task, work['requirements'], rates['labor'])
        if rate is None:
            assumptions.append(reason+'; стоимость работы неизвестна.')
        lines.append({'id': f"line-labor-{task['id']}", 'kind': 'labor', 'reference_id': task['id'],
                      'amount_minor': rate['amount_minor'] if rate else None,
                      'source_ids': rate['source_ids'] if rate else rate_sources})
    for d in proc['deliveries']:
        lines.append({'id': f"line-delivery-{d['id']}", 'kind': 'delivery', 'reference_id': d['id'],
                      'amount_minor': d['amount_minor'], 'source_ids': d['source_ids']})
    for line in lines:
        known_sources(context, line['source_ids'], f"Строка «{line['id']}»")
    payload = {'lines': lines, **calc.totals(lines, rule['percent'], brief['budget_minor'])}
    source_ids = {x for line in lines for x in line['source_ids']} | set(rule['source_ids'])
    assumptions = source_note(context, source_ids)+assumptions
    assumptions.append(f"Резерв {rule['percent']}% по правилу {rule['version']} начисляется на материалы, работу и доставку. "
                       'Запас материалов уже учтён в числе упаковок и в резерв не входит.')
    if payload['total_minor'] is None:
        assumptions.append('Есть неизвестные суммы: итог и статус бюджета не рассчитываются, неизвестное не считается нулём.')
    return payload, source_ids, assumptions


def procurement_handler(choices, rates=None, new_id=lambda: str(uuid4())):
    """choices: {requirement_id: offer_id} — выбор модели или человека; код проверяет каждый артикул."""
    rates = rates or load('rates.json')
    return stage('procurement', lambda context: build_procurement(context, dict(choices), rates), new_id)


def estimate_handler(rates=None, new_id=lambda: str(uuid4())):
    rates = rates or load('rates.json')
    return stage('estimate', lambda context: build_estimate(context, rates), new_id)


def need_with_waste(requirement):
    return calc.text(calc.with_waste(Decimal(requirement['base_quantity']['value']), Decimal(requirement['waste_percent'])))
