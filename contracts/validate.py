"""Проверка формы и согласованности общего комплекта, без LLM и сети."""
from __future__ import annotations
import argparse
import json
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parent
DEPENDENCIES = {
    'brief': set(), 'design_options': {'brief'},
    'design_selection': {'design_options'},
    'work_plan': {'brief', 'design_selection'},
    'procurement': {'work_plan'}, 'estimate': {'work_plan', 'procurement'},
    'schedule': {'brief', 'work_plan', 'procurement'},
    'audit': {'brief', 'design_options', 'design_selection', 'work_plan', 'procurement', 'estimate', 'schedule'},
}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def index(items, key='id'):
    result = {x[key]: x for x in items}
    require(len(result) == len(items), f'Повтор идентификатора: {key}')
    return result

def validate_schema(data, name):
    schema = json.loads((ROOT / 'schemas' / name).read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(data)

def validate_stage(data):
    validate_schema(data, 'stage-result.schema.json')
    status = data['status']
    require((data['artifact_id'] is not None) == (status == 'ok'), 'Документ допустим только при ok')
    require(bool(data['questions']) == (status == 'needs_input'), 'Вопросы допустимы только при needs_input')
    require((data['error'] is not None) == (status == 'failed'), 'Ошибка допустима только при failed')

def validate_stage_output(result, artifact, context):
    """Публичная граница приёма результата этапа координатором."""
    validate_stage(result)
    for key in ('schema_version', 'project_id', 'run_id', 'revision'):
        require(result[key] == context[key], 'Ответ относится к другому запуску или версии')
    require(result['stage'] == context['stage'], 'Ответ другого этапа')
    if result['status'] != 'ok':
        require(artifact is None, 'При вопросе или ошибке документ запрещён')
        return
    require(artifact is not None, 'При успехе нужен документ')
    schema = json.loads((ROOT / 'schemas' / 'plan.schema.json').read_text())
    Draft202012Validator(schema['properties']['artifacts']['items'], format_checker=FormatChecker()).validate(artifact)
    for key in ('schema_version', 'project_id', 'run_id', 'revision'):
        require(artifact[key] == result[key], 'Документ относится к другому запуску или версии')
    require(artifact['artifact_id'] == result['artifact_id'] and artifact['kind'] == result['stage'], 'Не совпадают документ и ответ этапа')
    require(set(artifact['input_artifact_ids']) == set(context['input_artifact_ids']), 'Подменены входные документы')

def validate_bundle(data):
    validate_schema(data, 'plan.schema.json')
    docs = index(data['artifacts'], 'artifact_id')
    kinds = index(data['artifacts'], 'kind')
    require(set(kinds) == set(DEPENDENCIES), 'Нужны все восемь видов документов')
    sources = index(data['sources'])
    offers = index(data['offers'])
    def source_refs(value):
        if isinstance(value, dict):
            if 'source_ids' in value:
                require(set(value['source_ids']) <= sources.keys(), 'Неизвестный источник')
            for child in value.values():
                source_refs(child)
        elif isinstance(value, list):
            for child in value:
                source_refs(child)
    source_refs(data)
    for doc in docs.values():
        require(all(doc[k] == data[k] for k in ('schema_version', 'project_id', 'run_id', 'revision')), 'Смешаны проекты, запуски или версии')
        refs = doc['input_artifact_ids']
        require(len(refs) == len(set(refs)) and set(refs) <= docs.keys(), 'Некорректные ссылки на входные документы')
        require({docs[x]['kind'] for x in refs} == DEPENDENCIES[doc['kind']], 'Неверные входы этапа')
    p = {kind: doc['payload'] for kind, doc in kinds.items()}
    brief, work, proc, est, sch, audit = (p[x] for x in ('brief', 'work_plan', 'procurement', 'estimate', 'schedule', 'audit'))
    room = brief['room']
    length, width, height = (Decimal(room[k]) for k in ('length_m', 'width_m', 'height_m'))
    require(min(length, width, height) > 0, 'Размеры должны быть положительными')
    require(Decimal(room['openings_m2']) <= 2*(length+width)*height, 'Проёмы больше стен')
    require(brief['deadline'] >= brief['start_date'], 'Окончание раньше начала')
    concepts = index(p['design_options']['concepts'])
    require(p['design_selection']['concept_id'] in concepts, 'Выбрана неизвестная концепция')
    chosen = concepts[p['design_selection']['concept_id']]
    require(set(brief['hard_preferences']) <= set(chosen['hard_preferences_met']), 'Потеряно обязательное пожелание')
    tasks, requirements = index(work['tasks']), index(work['requirements'])
    for task in tasks.values():
        require(Decimal(task['quantity']['value']) > 0 and Decimal(task['labor_hours']) > 0, 'Работа должна иметь положительные объём и трудозатраты')
        require(set(task['predecessor_ids']) <= tasks.keys(), 'Неизвестная предшествующая работа')
        require(len(task['predecessor_ids']) == len(set(task['predecessor_ids'])), 'Повтор зависимости работы')
    pending = set(tasks)
    while pending:
        ready = {t for t in pending if not (set(tasks[t]['predecessor_ids']) & pending)}
        require(ready, 'Цикл в графе работ')
        pending -= ready
    for req in requirements.values():
        require(set(req['task_ids']) <= tasks.keys(), 'Потребность ссылается на неизвестную работу')
        require(Decimal(req['base_quantity']['value']) > 0, 'Нулевая потребность')
    purchases, deliveries = index(proc['purchases']), index(proc['deliveries'])
    by_req = index(proc['purchases'], 'requirement_id')
    require(set(by_req) == set(requirements), 'Не все потребности покрыты покупками')
    by_supplier = index(proc['deliveries'], 'supplier_id')
    used_suppliers = set()
    for buy in purchases.values():
        require(buy['offer_id'] in offers, 'Неизвестное предложение')
        offer, req = offers[buy['offer_id']], requirements[buy['requirement_id']]
        used_suppliers.add(offer['supplier_id'])
        require(offer['category'] == req['category'] and offer['compatibility_key'] == req['compatibility_key'], 'Несовместимый товар')
        require(offer['pack_quantity']['unit'] == req['base_quantity']['unit'] == buy['purchased_quantity']['unit'], 'Не совпадают единицы')
        capacity = Decimal(offer['pack_quantity']['value'])
        require(capacity > 0, 'Пустая упаковка')
        need = Decimal(req['base_quantity']['value'])*(1+Decimal(req['waste_percent'])/100)
        count = int((need/capacity).to_integral_value(rounding=ROUND_CEILING))
        require(buy['pack_count'] == count, 'Неверное округление упаковок')
        require(Decimal(buy['purchased_quantity']['value']) == capacity*count, 'Неверный закупаемый объём')
        require(buy['unit_price_minor'] == offer['price_minor'] and buy['amount_minor'] == count*offer['price_minor'], 'Неверная цена покупки')
    require(set(by_supplier) == used_suppliers, 'Доставка не соответствует поставщикам')
    index(est['lines'])
    expected = {('material', x): b['amount_minor'] for x,b in purchases.items()}
    expected.update({('delivery', x): d['amount_minor'] for x,d in deliveries.items()})
    seen = set()
    for line in est['lines']:
        key = (line['kind'], line['reference_id'])
        require(key not in seen, 'Двойной учёт статьи')
        seen.add(key)
        if line['kind'] == 'labor':
            require(line['reference_id'] in tasks, 'Оплата неизвестной работы')
        else:
            require(key in expected and line['amount_minor'] == expected[key], 'Неверная строка материалов или доставки')
    require(seen == set(expected) | {('labor', x) for x in tasks}, 'Пропущена статья сметы')
    unknown = any(line['amount_minor'] is None for line in est['lines'])
    if unknown:
        require(all(est[x] is None for x in ('base_minor','reserve_minor','total_minor')) and est['budget_status']=='unknown', 'Неизвестные расходы нельзя считать нулём')
    else:
        base = sum(line['amount_minor'] for line in est['lines'])
        reserve = int((Decimal(base)*Decimal(est['reserve_percent'])/100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        require((est['base_minor'],est['reserve_minor'],est['total_minor']) == (base,reserve,base+reserve), 'Неверная сумма сметы')
        require(est['budget_status'] == ('satisfied' if base+reserve <= brief['budget_minor'] else 'violated'), 'Неверный статус бюджета')
    slots = index(sch['items'], 'task_id')
    require(set(slots) == set(tasks), 'График не покрывает работы')
    calendar = brief['calendar']; zone = ZoneInfo(calendar['timezone'])
    require(calendar['day_start_hour']+calendar['hours_per_day'] <= 24, 'Рабочий день выходит за полночь')
    times = {}
    for tid,slot in slots.items():
        start,end = (datetime.fromisoformat(slot[k].replace('Z','+00:00')).astimezone(zone) for k in ('start_at','finish_at'))
        require(start < end and start.date().isoformat() >= brief['start_date'], 'Некорректные даты работы')
        require((end-start).days <= 366, 'График длиннее года не поддерживается в MVP')
        seconds = Decimal(0); day = start.date()
        while day <= end.date():
            if day.weekday() in calendar['work_weekdays']:
                lo=datetime.combine(day,time(calendar['day_start_hour']),zone)
                hi=lo+timedelta(hours=calendar['hours_per_day'])
                seconds += Decimal(str(max(0,(min(end,hi)-max(start,lo)).total_seconds())))
            day += timedelta(days=1)
        require(seconds == Decimal(tasks[tid]['labor_hours'])*3600, 'Трудозатраты не совпадают с календарём')
        require(start.weekday() in calendar['work_weekdays'] and calendar['day_start_hour'] <= start.hour < calendar['day_start_hour']+calendar['hours_per_day'], 'Начало вне рабочего времени')
        require(end.weekday() in calendar['work_weekdays'] and calendar['day_start_hour'] < (end.hour+end.minute/60) <= calendar['day_start_hour']+calendar['hours_per_day'], 'Окончание вне рабочего времени')
        times[tid]=(start,end)
        for req in requirements.values():
            if tid in req['task_ids']:
                offer=offers[by_req[req['id']]['offer_id']];delivery=by_supplier[offer['supplier_id']]
                for available in (offer['available_on'],delivery['available_on']):
                    if available is not None:
                        require(start.date().isoformat() >= available, 'Работа раньше поставки')
    for tid,(start,end) in times.items():
        for pred in tasks[tid]['predecessor_ids']:
            wait=timedelta(seconds=float(Decimal(tasks[pred]['wait_after_hours'])*3600))
            require(start >= times[pred][1]+wait, 'Нарушена зависимость или технологическая пауза')
    ordered=sorted(times.values())
    require(all(a[1] <= b[0] for a,b in zip(ordered,ordered[1:])), 'Работы одной бригады пересекаются')
    finish=max(end+timedelta(seconds=float(Decimal(tasks[t]['wait_after_hours'])*3600)) for t,(_,end) in times.items())
    require(datetime.fromisoformat(sch['finish_at'].replace('Z','+00:00')) == finish, 'Неверное окончание ремонта')
    unknown_dates = any(x['available_on'] is None for x in deliveries.values()) or any(offers[b['offer_id']]['available_on'] is None for b in purchases.values())
    deadline_status='unknown' if unknown_dates else ('satisfied' if finish.date().isoformat() <= brief['deadline'] else 'violated')
    require(sch['deadline_status']==deadline_status, 'Неверный статус срока')
    index(audit['findings'])
    blocking=any(f['severity']=='blocking' for f in audit['findings'])
    for f in audit['findings']:
        require(set(f['artifact_ids']) <= docs.keys(), 'Замечание ссылается на неизвестный документ')
    require(not blocking or audit['status']=='failed', 'Существенная ошибка скрыта аудитом')
    unresolved = unknown or unknown_dates or room['surface_condition']=='unknown' or bool(work['unsupported_requests'])
    require(not unresolved or audit['status'] != 'passed', 'Неполные данные не могут пройти аудит')
    ready = not unresolved and not blocking and audit['status']=='passed' and est['budget_status']=='satisfied' and sch['deadline_status']=='satisfied'
    require(data['status']==('completed' if ready else 'constraints_unresolved'), 'Ложный итоговый статус')

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--stage', action='store_true')
    args=parser.parse_args()
    value=json.loads(args.file.read_text())
    (validate_stage if args.stage else validate_bundle)(value)
    print('Контракт и проверяемые условия соблюдены.')
