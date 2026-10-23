from copy import deepcopy

import pytest

from contracts.handoff import call_stage
from app.planning.schedule import build_schedule
from app.planning.stages import schedule_handler
from karina_helpers import bundle, payload, schedule_context


def compute(data=None):
    return build_schedule(schedule_context(data))[0]


def test_reference_schedule_matches_independently_written_dates():
    result = compute()
    assert [(x['task_id'], x['start_at'], x['finish_at']) for x in result['items']] == [
        ('prepare', '2026-10-26T09:00:00+03:00', '2026-10-26T17:00:00+03:00'),
        ('paint-task', '2026-10-27T09:00:00+03:00', '2026-10-27T17:00:00+03:00'),
        ('floor-task', '2026-10-28T09:00:00+03:00', '2026-10-28T17:00:00+03:00')]
    assert result['finish_at'] == '2026-10-28T17:00:00+03:00'
    assert result['deadline_status'] == 'satisfied'


def test_weekend_counts_for_wait_but_not_crew_hours():
    data = bundle()
    brief = payload(data, 'brief')
    brief.update(start_date='2026-10-30', deadline='2026-11-10')
    payload(data, 'work_plan')['tasks'][0]['wait_after_hours'] = '48'
    result = compute(data)
    assert result['items'][1]['start_at'] == '2026-11-02T09:00:00+03:00'
    assert result['finish_at'] == '2026-11-03T17:00:00+03:00'


def test_fractional_hours_and_work_spanning_days():
    data = bundle()
    tasks = payload(data, 'work_plan')['tasks']
    tasks[0]['labor_hours'] = '4.5'
    tasks[1]['labor_hours'] = '3.5'
    result = compute(data)
    assert result['items'][0]['finish_at'] == '2026-10-26T13:30:00+03:00'
    assert result['items'][1]['start_at'] == '2026-10-26T13:30:00+03:00'
    assert result['finish_at'] == '2026-10-27T17:00:00+03:00'
    tasks[0]['labor_hours'] = '10'
    tasks[1]['labor_hours'] = '8'
    result = compute(data)
    assert result['items'][0]['finish_at'] == '2026-10-27T11:00:00+03:00'
    assert result['items'][1]['finish_at'] == '2026-10-28T11:00:00+03:00'
    assert result['finish_at'] == '2026-10-29T17:00:00+03:00'


def test_last_pause_changes_project_finish_even_over_weekend():
    data = bundle()
    payload(data, 'work_plan')['tasks'][-1]['wait_after_hours'] = '96'
    result = compute(data)
    assert result['items'][-1]['finish_at'] == '2026-10-28T17:00:00+03:00'
    assert result['finish_at'] == '2026-11-01T17:00:00+03:00'
    assert result['deadline_status'] == 'violated'


def test_late_material_and_late_delivery_both_shift_work():
    data = bundle()
    next(o for o in data['offers'] if o['id'] == 'floor')['available_on'] = '2026-11-05'
    assert compute(data)['items'][-1]['start_at'] == '2026-11-05T09:00:00+03:00'
    payload(data, 'procurement')['deliveries'][0]['available_on'] = '2026-11-06'
    assert compute(data)['items'][0]['start_at'] == '2026-11-06T09:00:00+03:00'
    assert compute(data)['finish_at'] == '2026-11-10T17:00:00+03:00'


def test_ready_work_can_use_time_before_another_material_arrives():
    data = bundle()
    payload(data, 'work_plan')['tasks'][-1]['predecessor_ids'] = []
    next(o for o in data['offers'] if o['id'] == 'paint')['available_on'] = '2026-11-10'
    result = compute(data)
    assert [i['task_id'] for i in result['items']] == ['prepare', 'floor-task', 'paint-task']
    assert result['items'][1]['start_at'] == '2026-10-27T09:00:00+03:00'


def test_unknown_availability_is_explicitly_provisional():
    data = bundle()
    payload(data, 'procurement')['deliveries'][0]['available_on'] = None
    result, assumptions, details = build_schedule(schedule_context(data))
    assert result['deadline_status'] == 'unknown' and details['provisional']
    assert any('неизвестна' in text for text in assumptions)


@pytest.mark.parametrize('fault, message', [('cycle', 'Цикл'), ('unknown', 'Неизвестная'), ('duplicate', 'Повтор'), ('zero', 'положительный')])
def test_bad_work_plan_returns_failed_stage_without_artifact(fault, message):
    data = bundle()
    tasks = payload(data, 'work_plan')['tasks']
    if fault == 'cycle': tasks[0]['predecessor_ids'] = ['floor-task']
    if fault == 'unknown': tasks[0]['predecessor_ids'] = ['absent']
    if fault == 'duplicate': tasks.append(deepcopy(tasks[0]))
    if fault == 'zero': tasks[0]['labor_hours'] = '0'
    result, doc = call_stage(schedule_handler(), schedule_context(data))
    assert result['status'] == 'failed' and doc is None
    assert result['error']['code'] == 'INVALID_INPUT' and message in result['error']['message']


def test_handler_refuses_stale_context_and_does_not_mutate_input():
    context = schedule_context()
    next(d for d in context['input_artifacts'] if d['kind'] == 'brief')['revision'] += 1
    original = deepcopy(context)
    result, doc = call_stage(schedule_handler(), context)
    assert result['error']['code'] == 'STALE_REVISION' and doc is None
    assert context == original


def test_custom_calendar_and_midnight_end():
    data = bundle()
    payload(data, 'brief')['calendar'].update(work_weekdays=[0, 1, 2, 3, 4, 5, 6], day_start_hour=16, hours_per_day=8)
    result = compute(data)
    assert result['items'][0]['finish_at'] == '2026-10-27T00:00:00+03:00'
    assert result['items'][1]['start_at'] == '2026-10-27T16:00:00+03:00'


def test_planning_horizon_is_bounded():
    data = bundle()
    payload(data, 'work_plan')['tasks'][0]['labor_hours'] = '9000'
    result, doc = call_stage(schedule_handler(), schedule_context(data))
    assert result['error']['code'] == 'LIMIT_REACHED' and doc is None
