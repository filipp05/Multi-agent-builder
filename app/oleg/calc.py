"""Расчёты закупки и сметы без LLM: объёмы через Decimal, деньги в целых копейках."""
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from contracts.validate import require


def text(value: Decimal) -> str:
    """Десятичная строка договора: "22", "9.812", без экспоненты."""
    return format(value.normalize(), 'f')


def wall_area(room) -> Decimal:
    """Площадь стен под покраску: периметр × высота минус проёмы, м²."""
    length, width, height, openings = (Decimal(room[k]) for k in ('length_m', 'width_m', 'height_m', 'openings_m2'))
    gross = 2*(length+width)*height
    require(min(length, width, height) > 0, 'Размеры комнаты должны быть положительными')
    require(Decimal(0) <= openings <= gross, 'Проёмы больше площади стен')
    return gross - openings


def paint_litres(area_m2: Decimal, coats: int, litres_per_m2: Decimal) -> Decimal:
    """Чистая потребность в краске, л. Запас добавляется отдельно и один раз."""
    require(area_m2 > 0 and coats >= 1 and litres_per_m2 > 0, 'Некорректные данные для расчёта краски')
    return area_m2*coats*litres_per_m2


def with_waste(base: Decimal, waste_percent: Decimal) -> Decimal:
    require(base > 0 and waste_percent >= 0, 'Некорректная потребность или запас')
    return base*(1+waste_percent/100)


def pack_count(base: Decimal, waste_percent: Decimal, pack: Decimal) -> int:
    """Число упаковок = ⌈потребность × (1 + запас/100) / объём упаковки⌉."""
    require(pack > 0, 'Пустая упаковка')
    return int((with_waste(base, waste_percent)/pack).to_integral_value(rounding=ROUND_CEILING))


def reserve_minor(base_minor: int, percent: str) -> int:
    """Денежный резерв на всю базу, ROUND_HALF_UP до копейки."""
    return int((Decimal(base_minor)*Decimal(percent)/100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def mismatch(requirement, offer, currency):
    """Причина, по которой предложение не закрывает потребность, или None."""
    if offer['currency'] != currency:
        return f"Валюта предложения «{offer['id']}» ({offer['currency']}) не совпадает с валютой бюджета ({currency})"
    if offer['category'] != requirement['category']:
        return f"Предложение «{offer['id']}» другой категории: {offer['category']} вместо {requirement['category']}"
    if offer['compatibility_key'] != requirement['compatibility_key']:
        return f"Предложение «{offer['id']}» несовместимо: {offer['compatibility_key']} вместо {requirement['compatibility_key']}"
    if offer['pack_quantity']['unit'] != requirement['base_quantity']['unit']:
        return f"Не совпадают единицы «{offer['id']}»: {offer['pack_quantity']['unit']} вместо {requirement['base_quantity']['unit']}"
    if Decimal(offer['pack_quantity']['value']) <= 0:
        return f"Пустая упаковка у предложения «{offer['id']}»"
    return None


def purchase(requirement, offer):
    pack = Decimal(offer['pack_quantity']['value'])
    count = pack_count(Decimal(requirement['base_quantity']['value']), Decimal(requirement['waste_percent']), pack)
    return {'id': f"buy-{requirement['id']}", 'requirement_id': requirement['id'], 'offer_id': offer['id'],
            'pack_count': count, 'purchased_quantity': {'value': text(pack*count), 'unit': offer['pack_quantity']['unit']},
            'unit_price_minor': offer['price_minor'], 'amount_minor': count*offer['price_minor']}


def deliveries(used_offers, suppliers, fallback_source_ids):
    """Одна доставка на каждого использованного поставщика. Неизвестная цена или дата — None, не ноль."""
    by_supplier = {}
    for offer in used_offers:
        by_supplier.setdefault(offer['supplier_id'], []).append(offer)
    result = []
    for supplier_id in sorted(by_supplier):
        rate = suppliers.get(supplier_id)
        dates = [o['available_on'] for o in by_supplier[supplier_id]]
        result.append({'id': f'delivery-{supplier_id}', 'supplier_id': supplier_id,
                       'amount_minor': rate['delivery_minor'] if rate else None,
                       'available_on': None if None in dates else max(dates),
                       'source_ids': rate['source_ids'] if rate else list(fallback_source_ids)})
    return result


def labor_rate(task, requirements, labor):
    """Тариф работы по категории связанной потребности, или (None, причина)."""
    categories = {r['category'] for r in requirements if task['id'] in r['task_ids']}
    if len(categories) != 1:
        return None, f"Работа «{task['id']}» не связана ровно с одной категорией материалов; тариф не определён"
    category = categories.pop()
    rate = next((x for x in labor if x['category'] == category), None)
    if rate is None:
        return None, f"Нет тарифа работы для категории {category}"
    if rate['unit'] != task['quantity']['unit']:
        return None, f"Тариф {category} задан в {rate['unit']}, а работа «{task['id']}» — в {task['quantity']['unit']}"
    if Decimal(task['quantity']['value']) > Decimal(rate['max_quantity']):
        return None, f"Объём работы «{task['id']}» больше предела фиксированного тарифа {rate['max_quantity']} {rate['unit']}"
    return rate, None


def totals(lines, percent, budget_minor):
    """База, резерв, итог и статус бюджета. Любая неизвестная строка делает итог неизвестным."""
    if any(line['amount_minor'] is None for line in lines):
        return {'base_minor': None, 'reserve_percent': percent, 'reserve_minor': None, 'total_minor': None, 'budget_status': 'unknown'}
    base = sum(line['amount_minor'] for line in lines)
    reserve = reserve_minor(base, percent)
    return {'base_minor': base, 'reserve_percent': percent, 'reserve_minor': reserve, 'total_minor': base+reserve,
            'budget_status': 'satisfied' if base+reserve <= budget_minor else 'violated'}
