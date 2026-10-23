"""Перепроверка присланных procurement и estimate по правилам: пересчёт с нуля и сравнение по полям."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal

from app.oleg.stages import StageInputError, build_estimate, build_procurement


def finding(code, message, path):
    return {'code': code, 'message': message, 'path': path}


def verify(context, procurement, estimate, rates):
    """context — контекст этапа procurement. Возвращает список замечаний; пустой список — расхождений нет."""
    found = []
    purchases, deliveries = procurement['payload']['purchases'], procurement['payload']['deliveries']
    per_requirement = Counter(p['requirement_id'] for p in purchases)
    for rid, n in sorted(per_requirement.items()):
        if n > 1:
            found.append(finding('DUPLICATE_PURCHASE', f'Потребность «{rid}» закуплена {n} раза', f'procurement.purchases.{rid}'))
    choices = {p['requirement_id']: p['offer_id'] for p in purchases}
    try:
        expected_proc, _, _ = build_procurement(context, choices, rates)
    except StageInputError as e:
        return found+[finding('INVALID_CHOICE', str(e), ', '.join(e.field_paths) or 'procurement')]
    expected = {p['requirement_id']: p for p in expected_proc['purchases']}
    for p in purchases:
        want = expected[p['requirement_id']]
        path = f"procurement.purchases.{p['id']}"
        if p['pack_count'] < want['pack_count']:
            found.append(finding('PACKS_ROUNDED_DOWN', f"«{p['id']}»: {p['pack_count']} уп. вместо {want['pack_count']} — округление вниз не покрывает потребность", path))
        elif p['pack_count'] != want['pack_count']:
            found.append(finding('PACK_COUNT', f"«{p['id']}»: {p['pack_count']} уп. вместо {want['pack_count']}", path))
        if Decimal(p['purchased_quantity']['value']) != Decimal(want['purchased_quantity']['value']) or p['purchased_quantity']['unit'] != want['purchased_quantity']['unit']:
            found.append(finding('PURCHASED_QUANTITY', f"«{p['id']}»: закупаемый объём не равен числу упаковок × объём упаковки", path))
        if (p['unit_price_minor'], p['amount_minor']) != (want['unit_price_minor'], want['amount_minor']):
            found.append(finding('PURCHASE_PRICE', f"«{p['id']}»: цена не совпадает с каталогом или количеством упаковок", path))
    per_supplier = Counter(d['supplier_id'] for d in deliveries)
    want_delivery = {d['supplier_id']: d for d in expected_proc['deliveries']}
    for supplier, n in sorted(per_supplier.items()):
        if n > 1:
            found.append(finding('DUPLICATE_DELIVERY', f'Доставка от {supplier} учтена {n} раза; правило — одна доставка на поставщика', f'procurement.deliveries.{supplier}'))
    for supplier in sorted(set(want_delivery)-set(per_supplier)):
        found.append(finding('MISSING_DELIVERY', f'Нет доставки от используемого поставщика {supplier}', f'procurement.deliveries.{supplier}'))
    for supplier in sorted(set(per_supplier)-set(want_delivery)):
        found.append(finding('EXTRA_DELIVERY', f'Доставка от {supplier}, у которого ничего не покупается', f'procurement.deliveries.{supplier}'))
    for d in deliveries:
        want = want_delivery.get(d['supplier_id'])
        if want and d['amount_minor'] != want['amount_minor']:
            found.append(finding('DELIVERY_PRICE', f"Доставка от {d['supplier_id']}: {d['amount_minor']} вместо {want['amount_minor']} по тарифу", f"procurement.deliveries.{d['id']}"))

    est = estimate['payload']
    rule = rates['reserve']
    if Decimal(est['reserve_percent']) != Decimal(rule['percent']):
        found.append(finding('RESERVE_RULE', f"Ставка резерва {est['reserve_percent']}% не совпадает с правилом {rule['version']} ({rule['percent']}%); правило не менялось", 'estimate.reserve_percent'))
    accepted = deepcopy(context)
    accepted['input_artifacts'] = accepted['input_artifacts']+[{**procurement, 'payload': expected_proc}]
    expected_est, _, _ = build_estimate(accepted, rates)
    keys = Counter((line['kind'], line['reference_id']) for line in est['lines'])
    for (kind, ref), n in sorted(keys.items()):
        if n > 1:
            found.append(finding('DOUBLE_COUNT', f'Статья {kind} «{ref}» учтена {n} раза', f'estimate.lines.{ref}'))
    want_lines = {(line['kind'], line['reference_id']): line for line in expected_est['lines']}
    for kind, ref in sorted(set(want_lines)-set(keys)):
        found.append(finding('MISSING_LINE', f'В смете нет статьи {kind} «{ref}»', f'estimate.lines.{ref}'))
    for line in est['lines']:
        want = want_lines.get((line['kind'], line['reference_id']))
        if want is None:
            found.append(finding('EXTRA_LINE', f"Лишняя статья {line['kind']} «{line['reference_id']}»", f"estimate.lines.{line['id']}"))
        elif line['amount_minor'] != want['amount_minor']:
            found.append(finding('LINE_AMOUNT', f"Статья «{line['id']}»: {line['amount_minor']} вместо {want['amount_minor']}", f"estimate.lines.{line['id']}"))
    for key in ('base_minor', 'reserve_minor', 'total_minor', 'budget_status'):
        if est[key] != expected_est[key]:
            found.append(finding('TOTALS', f'{key}: {est[key]} вместо {expected_est[key]}', f'estimate.{key}'))
    return found
