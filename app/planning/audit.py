"""Independent reconstruction from primary documents, not a call to either author's calculator.

Only JSON shape validation is shared. Quantities, expected cost entries, money and
calendar occupancy are checked here with separate implementations.
"""
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from contracts.handoff import IDENTITY
from contracts.validate import DEPENDENCIES
from app.planning.common import documents, schema_errors
from app.input_validation import require_object_lists


def load_rules():
    return json.loads((Path(__file__).parent/'data/control-rules.json').read_text())


class Report:
    def __init__(self, docs):
        self.docs = docs
        self.findings = []
        self.incomplete = False

    def add(self, code, message, kind, item_ids=(), owner='karina', *, incomplete=False, warning=False):
        self.incomplete |= incomplete
        doc = self.docs[kind]
        self.findings.append({'id': f'finding-{len(self.findings)+1}',
                              'severity': 'warning' if warning or incomplete else 'blocking',
                              'code': code, 'message': message, 'artifact_ids': [doc['artifact_id']],
                              'item_ids': list(item_ids), 'owner': owner, 'source_ids': []})

    def result(self):
        failed = any(f['severity'] == 'blocking' for f in self.findings)
        return {'status': 'failed' if failed else ('incomplete' if self.incomplete else 'passed'),
                'findings': self.findings}


def inspect(context, rules=None):
    require_object_lists({'artifacts': documents(context), 'sources': context['sources'], 'offers': context['offers']})
    rules = rules or load_rules()
    docs = {a['kind']: a for a in documents(context)}
    report = Report(docs)
    ids = {a['artifact_id']: a for a in documents(context)}
    owners = {'brief': 'filipp', 'design_options': 'misha', 'design_selection': 'filipp',
              'work_plan': 'misha', 'procurement': 'oleg', 'estimate': 'oleg', 'schedule': 'karina'}
    malformed = False
    for kind, doc in docs.items():
        errors = list(schema_errors(doc, 'artifacts'))
        if errors:
            malformed = True
            report.add('DOCUMENT_SCHEMA', f'Документ {kind} не соответствует схеме. Исправьте поля документа.', kind, owner=owners[kind])
        if any(doc.get(k) != context[k] for k in IDENTITY):
            report.add('STALE_REVISION', 'Документ относится к другому проекту, запуску или версии. Пересчитайте его для текущего запуска.', kind, owner=owners[kind])
    for section in ('sources', 'offers'):
        if len({x.get('id') for x in context[section]}) != len(context[section]):
            report.add('DUPLICATE_ID', f'Повтор идентификатора в {section}. Устраните неоднозначность.', 'procurement', owner='oleg')
            malformed = True
        if any(next(schema_errors(x, section), None) for x in context[section]):
            report.add('REGISTRY_SCHEMA', f'Некорректный снимок {section}. Исправьте формат.', 'procurement', owner='oleg')
            malformed = True
    if malformed:
        return report.result()
    sources = {s['id'] for s in context['sources']}
    offers = {o['id']: o for o in context['offers']}

    def source_refs(value, kind):
        if isinstance(value, dict):
            missing = set(value.get('source_ids', []))-sources
            if missing:
                report.add('UNKNOWN_SOURCE', f'Неизвестные источники: {", ".join(sorted(missing))}. Добавьте источник или исправьте ссылку.', kind, owner=owners[kind])
            for child in value.values():
                source_refs(child, kind)
        elif isinstance(value, list):
            for child in value:
                source_refs(child, kind)
    for kind, doc in docs.items():
        source_refs(doc, kind)
        refs = doc['input_artifact_ids']
        if len(refs) != len(set(refs)) or not set(refs) <= ids.keys() or {ids[x]['kind'] for x in refs if x in ids} != DEPENDENCIES[kind]:
            report.add('INPUT_LINK', 'Неверные ссылки на входные документы. Восстановите зависимости текущей версии.', kind, owner=owners[kind])
    source_refs(context['offers'], 'procurement')
    brief, work, proc, est, sch = (docs[k]['payload'] for k in ('brief', 'work_plan', 'procurement', 'estimate', 'schedule'))

    def indexed(items, kind, key='id'):
        counts = Counter(x[key] for x in items)
        for item, n in counts.items():
            if n > 1:
                report.add('DUPLICATE_ID', f'Идентификатор {item} повторён {n} раза. Удалите дубль.', kind, [item], owners[kind])
        return {x[key]: x for x in items}

    tasks = indexed(work['tasks'], 'work_plan')
    reqs = indexed(work['requirements'], 'work_plan')
    buys = indexed(proc['purchases'], 'procurement')
    deliveries = indexed(proc['deliveries'], 'procurement')
    indexed(est['lines'], 'estimate')
    slots = indexed(sch['items'], 'schedule', 'task_id')
    # Independent scope check also protects imported and previously saved plans.
    allowed_exclusions = {'потолок', 'мебель', 'электрика', 'сантехника', 'перепланировка'}
    if any(x.strip().casefold() not in allowed_exclusions for x in brief['excluded_work']):
        report.add('UNVERIFIED_EXCLUSIONS',
                   'Исключённые работы не проверены для фиксированного сценария покраски и ламината. Требуется уточнить объём работ.',
                   'brief', owner='misha', incomplete=True)
    room = brief['room']
    length, width, height, openings = (Decimal(room[k]) for k in ('length_m', 'width_m', 'height_m', 'openings_m2'))
    if min(length, width, height) <= 0 or openings > 2*(length+width)*height:
        report.add('GEOMETRY', 'Размеры или площадь проёмов некорректны. Исправьте геометрию комнаты.', 'brief', owner='filipp')
    if brief['deadline'] < brief['start_date']:
        report.add('DATE_ORDER', 'Срок окончания раньше начала. Уточните даты.', 'brief', owner='filipp')
    if room['surface_condition'] == 'unknown':
        report.add('UNKNOWN_SURFACE', 'Состояние основания неизвестно. Требуется обследование перед окончательным планом.', 'brief', owner='misha', incomplete=True)
    if work['unsupported_requests']:
        report.add('UNSUPPORTED_WORK', 'Есть неподдерживаемые работы. Уточните объём ремонта.', 'work_plan', owner='misha', incomplete=True)
    concepts = indexed(docs['design_options']['payload']['concepts'], 'design_options')
    chosen = concepts.get(docs['design_selection']['payload']['concept_id'])
    if chosen is None:
        report.add('UNKNOWN_CONCEPT', 'Выбрана несуществующая концепция. Повторите выбор.', 'design_selection', owner='filipp')
    elif not set(brief['hard_preferences']) <= set(chosen['hard_preferences_met']):
        report.add('LOST_PREFERENCE', 'Концепция не покрывает обязательные пожелания. Исправьте концепцию или согласуйте изменение требований.', 'design_options', owner='misha')
    # Contract 0.1.0 contains no verified colour/decor or kit-content evidence.
    # A fluent model explanation must not turn this absence into a passed audit.
    if chosen and chosen.get('colors'):
        report.add('UNVERIFIED_FINISH',
                   'В каталоге не подтверждены оттенки краски и декор ламината для выбранной концепции. '
                   'Уточните соответствие палитре и стоимость колеровки; до этого план неполный.',
                   'procurement', owner='oleg', incomplete=True)
    if any(r['category'] == 'preparation' for r in work['requirements']):
        report.add('UNDISCLOSED_PREPARATION_KIT',
                   'Состав подготовительного комплекта не раскрыт. Подтвердите защитные расходники '
                   'и необходимые комплектующие для пола: что включено, уже есть или не требуется. '
                   'До подтверждения комплектность и итоговая стоимость предварительные.',
                   'work_plan', owner='misha', incomplete=True)

    categories = {r['category'] for r in reqs.values()}
    for category in set(rules['required_categories'])-categories:
        report.add('MISSING_WORK_CATEGORY', f'Нет потребности для обязательной категории {category}. Восстановите состав работ и материалов.', 'work_plan', owner='misha')
    for req in reqs.values():
        if not set(req['task_ids']) <= tasks.keys() or Decimal(req['base_quantity']['value']) <= 0:
            report.add('REQUIREMENT', 'Потребность имеет неверный объём или ссылку на работу. Исправьте план работ.', 'work_plan', [req['id']], 'misha')
    pending = set(tasks)
    for task in tasks.values():
        if not set(task['predecessor_ids']) <= tasks.keys() or len(task['predecessor_ids']) != len(set(task['predecessor_ids'])):
            report.add('TASK_DEPENDENCY', 'Неизвестный или повторный предшественник. Исправьте зависимости.', 'work_plan', [task['id']], 'misha')
        if Decimal(task['labor_hours']) <= 0 or Decimal(task['quantity']['value']) <= 0:
            report.add('TASK_QUANTITY', 'Трудозатраты и объём работы должны быть положительными.', 'work_plan', [task['id']], 'misha')
    while pending:
        ready = {t for t in pending if not set(tasks[t]['predecessor_ids']) & pending}
        if not ready:
            report.add('TASK_CYCLE', 'В графе работ цикл. Устраните циклические зависимости.', 'work_plan', sorted(pending), 'misha')
            break
        pending -= ready
    if set(rules['recognized_quantity_sources']) & set(docs['work_plan']['source_ids']):
        for category, expected, unit in (('paint', 2*(length+width)*height-openings, 'm2'), ('laminate', length*width, 'm2')):
            tids = {t for r in reqs.values() if r['category'] == category for t in r['task_ids'] if t in tasks}
            if tids and (any(tasks[t]['quantity']['unit'] != unit for t in tids) or sum(Decimal(tasks[t]['quantity']['value']) for t in tids) != expected):
                report.add('SURFACE_QUANTITY', f'Объём {category} не совпадает с геометрией. Пересчитайте площадь.', 'work_plan', sorted(tids), 'misha')
        paint_reqs = [r for r in reqs.values() if r['category'] == 'paint']
        paint_need = (2*(length+width)*height-openings)*Decimal(rules['paint_litres_per_m2'])
        if paint_reqs and (any(r['base_quantity']['unit'] != 'l' for r in paint_reqs) or sum(Decimal(r['base_quantity']['value']) for r in paint_reqs) != paint_need):
            report.add('PAINT_QUANTITY', 'Чистая потребность краски не совпадает с учебной нормой двух слоёв. Пересчитайте потребность без запаса.', 'work_plan', [r['id'] for r in paint_reqs], 'misha')
    else:
        report.add('UNVERIFIED_QUANTITY_RULE', 'Для этого источника нет независимого эталона расхода. Нужна проверка технологических норм.', 'work_plan', owner='misha', incomplete=True)

    per_req = Counter(b['requirement_id'] for b in buys.values())
    for rid in reqs:
        if per_req[rid] != 1:
            report.add('PURCHASE_COVERAGE', f'Потребность {rid} должна быть закуплена ровно один раз, получено {per_req[rid]}. Исправьте корзину.', 'procurement', [rid], 'oleg')
    expected_costs, used_suppliers = {}, set()
    for bid, buy in buys.items():
        req, offer = reqs.get(buy['requirement_id']), offers.get(buy['offer_id'])
        if req is None or offer is None:
            report.add('PURCHASE_REFERENCE', 'Покупка ссылается на неизвестную потребность или предложение. Исправьте ссылку.', 'procurement', [bid], 'oleg')
            continue
        used_suppliers.add(offer['supplier_id'])
        if offer['currency'] != brief['currency'] or (offer['category'], offer['compatibility_key']) != (req['category'], req['compatibility_key']):
            report.add('INCOMPATIBLE_OFFER', 'Товар не соответствует категории, совместимости или валюте. Выберите совместимое предложение.', 'procurement', [bid], 'oleg')
        capacity = Decimal(offer['pack_quantity']['value'])
        if capacity <= 0:
            report.add('EMPTY_PACK', 'Объём упаковки должен быть положительным.', 'procurement', [bid], 'oleg')
            continue
        count = int((Decimal(req['base_quantity']['value'])*(100+Decimal(req['waste_percent']))/(100*capacity)).to_integral_value(rounding=ROUND_CEILING))
        amount = count*offer['price_minor']
        if buy['pack_count'] != count or Decimal(buy['purchased_quantity']['value']) != count*capacity:
            report.add('PACK_QUANTITY', f'Нужно {count} целых упаковок с однократным запасом. Исправьте количество и закупаемый объём.', 'procurement', [bid], 'oleg')
        if buy['purchased_quantity']['unit'] != req['base_quantity']['unit'] or offer['pack_quantity']['unit'] != req['base_quantity']['unit']:
            report.add('UNIT_MISMATCH', 'Единицы потребности, упаковки и покупки различаются. Исправьте выбор товара.', 'procurement', [bid], 'oleg')
        if buy['unit_price_minor'] != offer['price_minor'] or buy['amount_minor'] != amount:
            report.add('PURCHASE_PRICE', f'Ожидаемая стоимость покупки {amount} коп. Исправьте цену по снимку каталога.', 'procurement', [bid], 'oleg')
        expected_costs[('material', bid)] = amount
    supplier_counts = Counter(d['supplier_id'] for d in deliveries.values())
    for supplier in used_suppliers | set(supplier_counts):
        if supplier_counts[supplier] != (1 if supplier in used_suppliers else 0):
            report.add('DELIVERY_COVERAGE', f'Для {supplier} нужна одна доставка при наличии покупок. Устраните пропуск, дубль или лишнюю доставку.', 'procurement', owner='oleg')
    for did, delivery in deliveries.items():
        value = delivery['amount_minor']
        if value is None:
            report.add('UNKNOWN_DELIVERY_COST', 'Стоимость доставки неизвестна. Уточните её; неизвестное не равно нулю.', 'procurement', [did], 'oleg', incomplete=True)
        elif set(delivery['source_ids']) & set(rules['recognized_price_sources']) and delivery['supplier_id'] in rules['delivery_minor']:
            if value != rules['delivery_minor'][delivery['supplier_id']]:
                report.add('DELIVERY_RATE', 'Стоимость доставки отличается от независимого учебного тарифа. Исправьте стоимость или обновите подтверждённый источник.', 'procurement', [did], 'oleg')
        else:
            report.add('UNVERIFIED_DELIVERY_RATE', 'Нет независимого тарифа доставки для этого источника. Подтвердите стоимость.', 'procurement', [did], 'oleg', incomplete=True)
        expected_costs[('delivery', did)] = value

    lines_by_key = {}
    for line in est['lines']:
        key = (line['kind'], line['reference_id'])
        if key in lines_by_key:
            report.add('DUPLICATE_LINE', 'Статья сметы учтена повторно. Удалите дубль.', 'estimate', [line['id']], 'oleg')
        lines_by_key[key] = line
        if line['amount_minor'] is None:
            report.add('UNKNOWN_LINE_COST', 'Сумма строки неизвестна. Уточните стоимость перед окончательным итогом.', 'estimate', [line['id']], 'oleg', incomplete=True)
    for tid, task in tasks.items():
        key = ('labor', tid)
        linked = {r['category'] for r in reqs.values() if tid in r['task_ids']}
        rate = rules['labor'].get(next(iter(linked))) if len(linked) == 1 else None
        line = lines_by_key.get(key)
        known = line and set(line['source_ids']) & set(rules['recognized_price_sources'])
        if rate and known and rate['unit'] == task['quantity']['unit'] and Decimal(task['quantity']['value']) <= Decimal(rate['max_quantity']):
            expected_costs[key] = rate['amount_minor']
        else:
            expected_costs[key] = line['amount_minor'] if line else None
            report.add('UNVERIFIED_LABOR_RATE', 'Нет применимого независимого тарифа работы. Уточните объём, единицы и источник.', 'estimate', [tid], 'oleg', incomplete=True)
    for key, value in expected_costs.items():
        line = lines_by_key.get(key)
        if line is None:
            report.add('MISSING_LINE', f'В смете нет статьи {key[0]} для {key[1]}. Добавьте строку.', 'estimate', [key[1]], 'oleg')
        elif line['amount_minor'] != value:
            report.add('LINE_AMOUNT', f'Статья {line["id"]}: ожидается {value} коп. Исправьте сумму по первичным данным.', 'estimate', [line['id']], 'oleg')
    for key in lines_by_key.keys()-expected_costs.keys():
        report.add('EXTRA_LINE', f'Лишняя статья {key[0]} для {key[1]}. Удалите её или восстановите основание.', 'estimate', [lines_by_key[key]['id']], 'oleg')
    if Decimal(est['reserve_percent']) != Decimal(rules['reserve_percent']):
        report.add('RESERVE_RULE', f'Резерв изменён без изменения правила: ожидается {rules["reserve_percent"]}%. Восстановите согласованную ставку.', 'estimate', owner='oleg')
    amounts = [line['amount_minor'] for line in est['lines']]
    unknown = any(v is None for v in amounts) or any(v is None for v in expected_costs.values())
    if unknown:
        if any(est[k] is not None for k in ('base_minor', 'reserve_minor', 'total_minor')) or est['budget_status'] != 'unknown':
            report.add('FALSE_KNOWN_TOTAL', 'При неизвестной статье база, резерв и итог должны быть неизвестны. Уберите неподтверждённый итог.', 'estimate', owner='oleg')
    else:
        base = sum(amounts)
        reserve = int((Decimal(base)*Decimal(rules['reserve_percent'])/100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        if (est['base_minor'], est['reserve_minor'], est['total_minor']) != (base, reserve, base+reserve):
            report.add('TOTALS', 'База, резерв или итог не совпадают с независимым пересчётом. Пересчитайте смету.', 'estimate', owner='oleg')
        budget_status = 'satisfied' if base+reserve <= brief['budget_minor'] else 'violated'
        if est['budget_status'] != budget_status:
            report.add('BUDGET_STATUS', 'Статус бюджета не соответствует сумме. Исправьте статус.', 'estimate', owner='oleg')
        if budget_status == 'violated':
            report.add('BUDGET_EXCEEDED', f'Бюджет превышен на {base+reserve-brief["budget_minor"]} коп. Согласуйте компромисс или новый бюджет.', 'estimate', owner='filipp', warning=True)
    _inspect_schedule(report, brief, tasks, reqs, buys, deliveries, offers, slots, sch)
    return report.result()


def _inspect_schedule(report, brief, tasks, reqs, buys, deliveries, offers, slots, schedule):
    """Integrate working intervals independently; deliberately does not rebuild the schedule."""
    calendar = brief['calendar']
    zone = ZoneInfo(calendar['timezone'])
    day_hours = timedelta(hours=calendar['hours_per_day'])
    if calendar['day_start_hour']+calendar['hours_per_day'] > 24:
        report.add('WORK_CALENDAR', 'Рабочий день выходит за полночь. Исправьте календарь.', 'brief', owner='filipp')
        return
    if set(slots) != set(tasks):
        report.add('SCHEDULE_COVERAGE', 'График должен покрывать все работы ровно один раз. Исправьте состав графика.', 'schedule')
    times = {}
    for tid, slot in slots.items():
        if tid not in tasks:
            continue
        start, end = (datetime.fromisoformat(slot[k].replace('Z', '+00:00')).astimezone(zone) for k in ('start_at', 'finish_at'))
        if not start < end or start.date().isoformat() < brief['start_date'] or (end-start).days > 366:
            report.add('SCHEDULE_INTERVAL', 'Некорректный интервал работы. Исправьте даты.', 'schedule', [tid])
            continue
        seconds = Decimal(0)
        day = start.date()
        start_valid = end_valid = False
        while day <= end.date():
            lower = datetime.combine(day, time(calendar['day_start_hour']), zone)
            upper = lower+day_hours
            if day.weekday() in calendar['work_weekdays']:
                start_valid |= lower <= start < upper
                end_valid |= lower < end <= upper
                seconds += Decimal(str(max(0, (min(end, upper)-max(start, lower)).total_seconds())))
            day += timedelta(days=1)
        if not start_valid or not end_valid or seconds != Decimal(tasks[tid]['labor_hours'])*3600:
            report.add('WORKING_HOURS', 'Интервал не соответствует рабочему календарю или трудозатратам. Перестройте график.', 'schedule', [tid])
        times[tid] = start, end
    ordered = sorted((a, b, tid) for tid, (a, b) in times.items())
    last_end = None
    for start, end, tid in ordered:
        if last_end and start < last_end:
            report.add('CREW_OVERLAP', 'Работы одной бригады пересекаются. Перенесите работу.', 'schedule', [tid])
        last_end = max(last_end, end) if last_end else end
    for tid, (start, end) in times.items():
        for pred in tasks[tid]['predecessor_ids']:
            if pred in times and start < times[pred][1]+timedelta(seconds=float(Decimal(tasks[pred]['wait_after_hours'])*3600)):
                report.add('TECHNOLOGICAL_WAIT', 'Нарушена зависимость или технологическая пауза. Сдвиньте начало работы.', 'schedule', [pred, tid])
    deliveries_by_supplier = {d['supplier_id']: d for d in deliveries.values()}
    unknown_dates = any(d['available_on'] is None for d in deliveries.values())
    for buy in buys.values():
        offer = offers.get(buy['offer_id'])
        req = reqs.get(buy['requirement_id'])
        if not offer or not req:
            continue
        delivery = deliveries_by_supplier.get(offer['supplier_id'])
        values = [offer['available_on'], delivery['available_on'] if delivery else None]
        unknown_dates |= None in values
        for tid in req['task_ids']:
            if tid in times and any(value is not None and times[tid][0].date().isoformat() < value for value in values):
                report.add('BEFORE_DELIVERY', 'Работа начинается до доступности материала или доставки. Сдвиньте график или измените поставку.', 'schedule', [tid])
    if unknown_dates:
        report.add('UNKNOWN_DELIVERY_DATE', 'Дата доступности материала или доставки неизвестна. График предварительный.', 'schedule', incomplete=True)
    if set(times) == set(tasks):
        finish = max(end+timedelta(seconds=float(Decimal(tasks[t]['wait_after_hours'])*3600)) for t, (_, end) in times.items())
        if datetime.fromisoformat(schedule['finish_at'].replace('Z', '+00:00')) != finish:
            report.add('FINISH_WITH_WAIT', 'Окончание ремонта не включает все работы и последнюю паузу. Исправьте итоговую дату.', 'schedule')
        deadline_status = 'unknown' if unknown_dates else ('satisfied' if finish.date().isoformat() <= brief['deadline'] else 'violated')
        if schedule['deadline_status'] != deadline_status:
            report.add('DEADLINE_STATUS', 'Статус срока не соответствует проверенному графику. Исправьте статус.', 'schedule')
        if finish.date().isoformat() > brief['deadline']:
            report.add('DEADLINE_EXCEEDED', 'Расчётное окончание позже желаемого срока. Согласуйте дату или допустимое изменение плана.', 'schedule', owner='filipp', warning=True)
