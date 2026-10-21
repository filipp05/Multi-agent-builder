import json

import pytest

from contracts.handoff import call_stage, verify_handoffs
from app.oleg import service
from app.oleg.agents import candidates
from app.oleg.stages import estimate_handler, procurement_handler
from oleg_helpers import EXAMPLE_CHOICES, RATES, bundle, estimate_context, procurement_context, raw_example


def run_stages(choices, data=None):
    data = data or bundle()
    result, proc = call_stage(procurement_handler(choices, RATES), procurement_context(data))
    assert result['status'] == 'ok', result['error']
    est_result, est = call_stage(estimate_handler(RATES), estimate_context(proc, data))
    assert est_result['status'] == 'ok', est_result['error']
    return proc, est


def failure(choices, context=None):
    result, artifact = call_stage(procurement_handler(choices, RATES), context or procurement_context())
    assert result['status'] == 'failed' and artifact is None
    assert result['error']['code'] == 'INVALID_INPUT'
    return result['error']


def test_control_example_is_74250_and_fits_shared_bundle():
    proc, est = run_stages(EXAMPLE_CHOICES)
    payload = est['payload']
    by_kind = {k: sum(x['amount_minor'] for x in payload['lines'] if x['kind'] == k) for k in ('material', 'labor', 'delivery')}
    assert by_kind == {'material': 3000000, 'labor': 3500000, 'delivery': 250000}
    assert (payload['base_minor'], payload['reserve_minor'], payload['total_minor']) == (6750000, 675000, 7425000)
    assert payload['budget_status'] == 'satisfied'
    assert [p['pack_count'] for p in proc['payload']['purchases']] == [1, 2, 10]
    data = bundle()
    assembled = service.assemble_bundle(raw_example() | {'offers': data['offers']}, proc, est, data['sources']+RATES['sources'])
    assert len(verify_handoffs(assembled)) == 8


def test_over_budget_is_a_valid_result_not_an_error():
    data = bundle()
    next(a for a in data['artifacts'] if a['kind'] == 'brief')['payload']['budget_minor'] = 7000000
    _, est = run_stages(EXAMPLE_CHOICES, data)
    assert est['payload']['total_minor'] == 7425000 and est['payload']['budget_status'] == 'violated'


def test_unknown_delivery_is_not_zero():
    proc, est = run_stages({**EXAMPLE_CHOICES, 'paint-req': 'paint-s3'})
    unknown = [d for d in proc['payload']['deliveries'] if d['supplier_id'] == 'store-3']
    assert unknown[0]['amount_minor'] is None and unknown[0]['available_on'] is None
    payload = est['payload']
    assert (payload['base_minor'], payload['reserve_minor'], payload['total_minor'], payload['budget_status']) == (None, None, None, 'unknown')
    assert any('store-3' in a and 'неизвестна' in a for a in proc['assumptions'])


def test_free_delivery_is_confirmed_zero_and_pack_size_changes_basket():
    proc, est = run_stages({'prep-req': 'primer-s2', 'paint-req': 'paint-s2', 'floor-req': 'floor-s2'})
    assert [d['amount_minor'] for d in proc['payload']['deliveries']] == [0]
    assert [p['pack_count'] for p in proc['payload']['purchases']] == [1, 4, 11]
    assert est['payload']['total_minor'] == 7271000


def test_delivery_counted_once_per_supplier():
    proc, _ = run_stages({'prep-req': 'primer-s1-deep', 'paint-req': 'paint-s1-matte', 'floor-req': 'floor-s1-oak'})
    assert [d['supplier_id'] for d in proc['payload']['deliveries']] == ['store-1']
    proc, _ = run_stages({**EXAMPLE_CHOICES, 'floor-req': 'floor-s2'})
    assert [d['supplier_id'] for d in proc['payload']['deliveries']] == ['store-1', 'store-2']


@pytest.mark.parametrize('offer_id, message', [
    ('no-such-sku', 'Неизвестный артикул'),
    ('floor-s1-wet', 'несовместимо'),
    ('paint-s1-facade', 'несовместимо'),
    ('paint-s2-kg', 'единицы'),
    ('primer', 'другой категории'),
])
def test_invalid_choice_is_refused_with_field_path(offer_id, message):
    req = 'floor-req' if offer_id.startswith('floor') else 'paint-req'
    error = failure({**EXAMPLE_CHOICES, req: offer_id})
    assert message in error['message'] and error['field_paths'] == [f'choices.{req}']


def test_missing_and_extra_choices_are_refused():
    assert 'Нет выбранного' in failure({'prep-req': 'primer', 'paint-req': 'paint'})['message']
    assert 'неизвестной потребности' in failure({**EXAMPLE_CHOICES, 'ghost': 'paint'})['message']


def test_currency_mismatch_is_refused():
    data = bundle()
    next(o for o in data['offers'] if o['id'] == 'paint')['currency'] = 'USD'
    assert 'Валюта' in failure(EXAMPLE_CHOICES, procurement_context(data))['message']


def test_rates_source_must_be_in_snapshot():
    data = bundle()
    assert 'отсутствуют в снимке источников' in failure(EXAMPLE_CHOICES, procurement_context(data, sources=data['sources']))['message']


def test_labor_without_applicable_tariff_is_unknown():
    data = bundle()
    work = next(a for a in data['artifacts'] if a['kind'] == 'work_plan')['payload']
    next(t for t in work['tasks'] if t['id'] == 'floor-task')['quantity']['value'] = '30'
    _, est = run_stages(EXAMPLE_CHOICES, data)
    labor = {x['reference_id']: x['amount_minor'] for x in est['payload']['lines'] if x['kind'] == 'labor'}
    assert labor['floor-task'] is None and est['payload']['total_minor'] is None
    assert any('floor-task' in a for a in est['assumptions'])


def test_catalog_snapshot_is_dated_sourced_and_has_analogs():
    catalog = json.loads((service.CONTRACTS.parent/'app/oleg/data/catalog.json').read_text(encoding='utf-8'))
    assert len(catalog['offers']) >= 12
    sources = {s['id']: s for s in catalog['sources']}
    assert all(set(o['source_ids']) <= sources.keys() for o in catalog['offers'])
    assert all(s['kind'] in ('synthetic', 'observed') and s['captured_at'] for s in sources.values())
    options = candidates(procurement_context(), RATES)
    assert {x['offer_id'] for x in options['floor-req']} == {'floor', 'floor-s1-oak', 'floor-s2', 'floor-s3'}
    assert {x['offer_id'] for x in options['paint-req']} == {'paint', 'paint-s1-matte', 'paint-s2', 'paint-s3'}
