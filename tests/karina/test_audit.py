from copy import deepcopy

import pytest

from contracts.handoff import call_stage
from app.karina import service
from app.karina.audit import inspect
from app.karina.stages import audit_handler
from karina_helpers import bundle, payload


def review(data):
    return inspect(service.audit_context(service.snapshot(data)))


def codes(result):
    return {f['code'] for f in result['findings']}


def test_reference_numbers_do_not_confirm_missing_material_properties():
    result = review(bundle())
    assert result['status'] == 'incomplete'
    assert codes(result) == {'UNVERIFIED_FINISH', 'UNDISCLOSED_PREPARATION_KIT'}


@pytest.mark.parametrize('fault, expected', [
    ('missing-line', 'MISSING_LINE'), ('duplicate-line', 'DUPLICATE_LINE'),
    ('labor', 'LINE_AMOUNT'), ('reserve', 'RESERVE_RULE'), ('packs', 'PACK_QUANTITY'),
    ('delivery', 'DELIVERY_COVERAGE'), ('delivery-price', 'DELIVERY_RATE'),
    ('stale', 'STALE_REVISION'), ('source', 'UNKNOWN_SOURCE'), ('link', 'INPUT_LINK'),
    ('cycle', 'TASK_CYCLE'), ('overlap', 'CREW_OVERLAP'), ('late', 'BEFORE_DELIVERY'),
    ('wait', 'TECHNOLOGICAL_WAIT'), ('last-wait', 'FINISH_WITH_WAIT'),
    ('geometry', 'SURFACE_QUANTITY'), ('paint', 'PAINT_QUANTITY'),
    ('missing-work', 'MISSING_WORK_CATEGORY'), ('unit', 'UNIT_MISMATCH'),
    ('concept', 'UNKNOWN_CONCEPT'), ('deadline', 'DEADLINE_STATUS'), ('money', 'TOTALS')])
def test_mutations_are_reported_with_addressed_findings(fault, expected):
    data = bundle()
    work, proc, est, sch = (payload(data, k) for k in ('work_plan', 'procurement', 'estimate', 'schedule'))
    if fault == 'missing-line': est['lines'].pop()
    if fault == 'duplicate-line': est['lines'].append({**est['lines'][0], 'id': 'another-material-line'})
    if fault == 'labor':
        est['lines'][3]['amount_minor'] -= 100000
        est.update(base_minor=6650000, reserve_minor=665000, total_minor=7315000)
    if fault == 'reserve': est.update(reserve_percent='5', reserve_minor=337500, total_minor=7087500)
    if fault == 'packs': proc['purchases'][-1].update(pack_count=9, purchased_quantity={'value': '19.8', 'unit': 'm2'}, amount_minor=1800000)
    if fault == 'delivery': proc['deliveries'].append({**proc['deliveries'][0], 'id': 'duplicate-delivery'})
    if fault == 'delivery-price': proc['deliveries'][0]['amount_minor'] = 0
    if fault == 'stale': data['artifacts'][0]['revision'] = 2
    if fault == 'source': est['lines'][0]['source_ids'] = ['not-registered']
    if fault == 'link': data['artifacts'][4]['input_artifact_ids'] = []
    if fault == 'cycle': work['tasks'][0]['predecessor_ids'] = ['floor-task']
    if fault == 'overlap': sch['items'][1].update(start_at=sch['items'][0]['start_at'], finish_at=sch['items'][0]['finish_at'])
    if fault == 'late': proc['deliveries'][0]['available_on'] = '2026-11-10'
    if fault == 'wait': work['tasks'][1]['wait_after_hours'] = '48'
    if fault == 'last-wait': work['tasks'][-1]['wait_after_hours'] = '48'
    if fault == 'geometry': work['tasks'][1]['quantity']['value'] = '45.6'
    if fault == 'paint': work['requirements'][1]['base_quantity']['value'] = '4.46'
    if fault == 'missing-work': work['requirements'].pop()
    if fault == 'unit': proc['purchases'][0]['purchased_quantity']['unit'] = 'm2'
    if fault == 'concept': payload(data, 'design_selection')['concept_id'] = 'missing'
    if fault == 'deadline': payload(data, 'brief')['deadline'] = '2026-10-26'
    if fault == 'money': est['total_minor'] -= 1
    result = review(data)
    assert result['status'] == 'failed' and expected in codes(result)
    finding = next(f for f in result['findings'] if f['code'] == expected)
    assert finding['severity'] == 'blocking' and finding['artifact_ids'] and finding['message'] and finding['owner']


def test_correct_unknown_delivery_stays_incomplete_not_zero():
    data = bundle()
    payload(data, 'procurement')['deliveries'][0]['amount_minor'] = None
    est = payload(data, 'estimate')
    est['lines'][-1]['amount_minor'] = None
    est.update(base_minor=None, reserve_minor=None, total_minor=None, budget_status='unknown')
    result = review(data)
    assert result['status'] == 'incomplete' and 'UNKNOWN_DELIVERY_COST' in codes(result)
    assert 'FALSE_KNOWN_TOTAL' not in codes(result)


def test_unknown_hidden_as_zero_is_blocking():
    data = bundle()
    payload(data, 'procurement')['deliveries'][0]['amount_minor'] = None
    result = review(data)
    assert result['status'] == 'failed' and 'FALSE_KNOWN_TOTAL' in codes(result)


@pytest.mark.parametrize('fault, expected', [('surface', 'UNKNOWN_SURFACE'), ('date', 'UNKNOWN_DELIVERY_DATE')])
def test_unknown_data_prevents_pass(fault, expected):
    data = bundle()
    if fault == 'surface': payload(data, 'brief')['room']['surface_condition'] = 'unknown'
    else:
        data['offers'][0]['available_on'] = None
        payload(data, 'schedule')['deadline_status'] = 'unknown'
    result = review(data)
    assert result['status'] == 'incomplete' and expected in codes(result)


def test_expensive_correct_plan_is_distinct_from_arithmetic_error():
    data = bundle()
    payload(data, 'brief')['budget_minor'] = 7000000
    payload(data, 'estimate')['budget_status'] = 'violated'
    result = review(data)
    assert result['status'] == 'incomplete'
    assert codes(result) == {'BUDGET_EXCEEDED', 'UNVERIFIED_FINISH', 'UNDISCLOSED_PREPARATION_KIT'}
    assert result['findings'][0]['severity'] == 'warning'


def test_program_only_handler_cannot_confirm_overall_success():
    context = service.audit_context(service.snapshot(bundle()))
    result, doc = call_stage(audit_handler(), context)
    assert result['status'] == 'ok' and doc['payload']['status'] == 'incomplete'
    _, checked = call_stage(audit_handler(semantic_findings=[]), context)
    assert checked['payload']['status'] == 'incomplete'
    assert codes(checked['payload']) == {'UNVERIFIED_FINISH', 'UNDISCLOSED_PREPARATION_KIT'}


def test_malformed_payload_is_reported_without_arithmetic_crash():
    data = bundle()
    payload(data, 'estimate')['reserve_percent'] = 'NaN'
    result = review(data)
    assert result['status'] == 'failed' and 'DOCUMENT_SCHEMA' in codes(result)
