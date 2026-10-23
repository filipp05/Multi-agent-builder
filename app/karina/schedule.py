"""One-crew list scheduling. All dates are computed here, never by an LLM."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.karina.common import payloads, require, unique, validate_context

MAX_DAYS = 366


def duration(hours):
    microseconds = Decimal(hours)*3_600_000_000
    require(microseconds == microseconds.to_integral_value(), 'Слишком высокая точность длительности', 'labor_hours')
    require(0 <= microseconds <= MAX_DAYS*86_400_000_000,
            'Длительность превышает год', 'labor_hours', 'LIMIT_REACHED')
    return timedelta(microseconds=int(microseconds))


class WorkCalendar:
    def __init__(self, calendar, start_date):
        self.zone = ZoneInfo(calendar['timezone'])
        self.weekdays = set(calendar['work_weekdays'])
        self.hour = calendar['day_start_hour']
        self.hours = calendar['hours_per_day']
        require(self.weekdays and self.hour+self.hours <= 24,
                'Рабочий день должен помещаться в сутки', 'brief.calendar')
        self.first = self.day_start(date.fromisoformat(start_date))
        self.limit = self.first + timedelta(days=MAX_DAYS)

    def day_start(self, day):
        return datetime.combine(day, time(self.hour), self.zone)

    def next_work(self, moment):
        while moment <= self.limit:
            start = self.day_start(moment.date())
            end = start + timedelta(hours=self.hours)
            if moment.weekday() in self.weekdays and moment < end:
                return max(moment, start)
            moment = self.day_start(moment.date()+timedelta(days=1))
        require(False, 'График выходит за предел одного года', 'schedule', 'LIMIT_REACHED')

    def finish(self, start, work):
        cursor, remaining = start, work
        while remaining > timedelta(0):
            cursor = self.next_work(cursor)
            available = self.day_start(cursor.date())+timedelta(hours=self.hours)-cursor
            if remaining <= available:
                return cursor+remaining
            remaining -= available
            cursor += available
        return cursor


def build_schedule(context):
    validate_context(context, 'schedule')
    data = payloads(context)
    brief, work, procurement = (data[k] for k in ('brief', 'work_plan', 'procurement'))
    require(brief['deadline'] >= brief['start_date'], 'Окончание раньше начала', 'brief.deadline')
    room = brief['room']
    require(all(Decimal(room[k]) > 0 for k in ('length_m', 'width_m', 'height_m')),
            'Размеры комнаты должны быть положительными', 'brief.room')
    require(Decimal(room['openings_m2']) <= 2*(Decimal(room['length_m'])+Decimal(room['width_m']))*Decimal(room['height_m']),
            'Проёмы больше площади стен', 'brief.room.openings_m2')
    cal = WorkCalendar(brief['calendar'], brief['start_date'])
    tasks = unique(work['tasks'], path='work_plan.tasks')
    require(len(tasks) <= 200, 'Слишком много работ для первого MVP', 'work_plan.tasks', 'LIMIT_REACHED')
    requirements = unique(work['requirements'], path='work_plan.requirements')
    purchases = unique(procurement['purchases'], 'requirement_id', 'procurement.purchases')
    unique(procurement['purchases'], path='procurement.purchases')
    deliveries = unique(procurement['deliveries'], 'supplier_id', 'procurement.deliveries')
    unique(procurement['deliveries'], path='procurement.deliveries')
    offers = unique(context['offers'], path='offers')
    require(set(purchases) == set(requirements), 'Не все потребности покрыты покупками', 'procurement.purchases')
    for task in tasks.values():
        require(Decimal(task['labor_hours']) > 0 and Decimal(task['quantity']['value']) > 0,
                'Работа должна иметь положительный объём и трудозатраты', f'work_plan.tasks.{task["id"]}')
        require(set(task['predecessor_ids']) <= tasks.keys(),
                'Неизвестная предшествующая работа', f'work_plan.tasks.{task["id"]}.predecessor_ids')
        require(len(task['predecessor_ids']) == len(set(task['predecessor_ids'])),
                'Повтор зависимости работы', f'work_plan.tasks.{task["id"]}.predecessor_ids')
    available = {tid: cal.first for tid in tasks}
    supply_notes = {tid: [] for tid in tasks}
    assumptions = []
    unknown_dates = False
    for rid, req in requirements.items():
        require(set(req['task_ids']) <= tasks.keys(), 'Потребность ссылается на неизвестную работу', f'requirements.{rid}')
        buy = purchases[rid]
        require(buy['offer_id'] in offers, 'Покупка ссылается на неизвестный товар', f'purchases.{buy["id"]}')
        offer = offers[buy['offer_id']]
        require(offer['supplier_id'] in deliveries, 'Нет доставки поставщика', 'procurement.deliveries')
        delivery = deliveries[offer['supplier_id']]
        for label, value in ((f'товар {offer["id"]}', offer['available_on']),
                             (f'доставка {delivery["id"]}', delivery['available_on'])):
            if value is None:
                unknown_dates = True
                assumptions.append(f'Дата доступности ({label}) неизвестна. Размещение работ предварительное; срок не подтверждён.')
            else:
                when = cal.day_start(date.fromisoformat(value))
                for tid in req['task_ids']:
                    available[tid] = max(available[tid], when)
                    supply_notes[tid].append(f'{label}: не раньше {value}')
    used_suppliers = {offers[b['offer_id']]['supplier_id'] for b in purchases.values()}
    require(set(deliveries) == used_suppliers, 'Лишняя доставка неиспользуемого поставщика', 'procurement.deliveries')
    # A cycle must be reported even when every affected material date is unknown.
    remaining, ordered = set(tasks), []
    while remaining:
        ready = [tid for tid in tasks if tid in remaining and not set(tasks[tid]['predecessor_ids']) & remaining]
        require(ready, 'Цикл в графе работ', 'work_plan.tasks.predecessor_ids')
        remaining.difference_update(ready)
        ordered.extend(ready)
    remaining = set(ordered)
    positions = {tid: n for n, tid in enumerate(tasks)}
    slots, rows, ends = [], [], {}
    crew_free = cal.first
    while remaining:
        candidates = []
        for tid in remaining:
            task = tasks[tid]
            if not set(task['predecessor_ids']) <= ends.keys():
                continue
            dependency = max((ends[p]+duration(tasks[p]['wait_after_hours']) for p in task['predecessor_ids']), default=cal.first)
            earliest = max(crew_free, dependency, available[tid], cal.first)
            candidates.append((cal.next_work(earliest), positions[tid], tid, dependency))
        start, _, tid, dependency = min(candidates)
        task = tasks[tid]
        finish = cal.finish(start, duration(task['labor_hours']))
        require(finish <= cal.limit, 'График выходит за предел одного года', 'schedule', 'LIMIT_REACHED')
        slot = {'task_id': tid, 'start_at': start.isoformat(), 'finish_at': finish.isoformat(), 'crew_id': 'crew-1'}
        slots.append(slot)
        rows.append({**slot, 'title': task['title'], 'predecessor_ids': task['predecessor_ids'],
                     'labor_hours': task['labor_hours'], 'wait_after_hours': task['wait_after_hours'],
                     'ready_after_predecessors': dependency.isoformat(), 'material_ready_at': available[tid].isoformat(),
                     'crew_free_at': crew_free.isoformat(), 'supply_notes': list(dict.fromkeys(supply_notes[tid]))})
        ends[tid] = finish
        crew_free = finish
        remaining.remove(tid)
    finish = max(ends[tid]+duration(tasks[tid]['wait_after_hours']) for tid in tasks)
    require(finish <= cal.limit, 'Последняя пауза выходит за предел одного года', 'schedule.finish_at', 'LIMIT_REACHED')
    deadline = date.fromisoformat(brief['deadline'])
    status = 'unknown' if unknown_dates else ('satisfied' if finish.date() <= deadline else 'violated')
    assumptions.append('Доступность материалов трактуется как поставка к началу рабочего дня указанной даты. Одна бригада; праздники и перерывы не моделируются.')
    payload = {'items': slots, 'finish_at': finish.isoformat(), 'deadline_status': status}
    details = {'rows': rows, 'deadline': brief['deadline'], 'delay_days': max(0, (finish.date()-deadline).days),
               'calendar': brief['calendar'], 'provisional': unknown_dates}
    return payload, list(dict.fromkeys(assumptions)), details
