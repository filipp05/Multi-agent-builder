import asyncio
from copy import deepcopy

from contracts.validate import validate_bundle
from app.procurement import calc, service
from oleg_helpers import EXAMPLE_CHOICES, RATES, bundle, raw_example


def control():
    data = bundle()
    out = asyncio.run(service.run(data, choices=EXAMPLE_CHOICES))
    return data, out['procurement'], out['estimate'], out['sources']


def codes(data, proc, est):
    return {f['code'] for f in service.verify(data, proc, est, RATES)}


def retotal(est, percent=None):
    """Согласованно пересчитать итоги после подмены, чтобы подмена не выдала себя арифметикой."""
    est['payload'].update(calc.totals(est['payload']['lines'], percent or est['payload']['reserve_percent'], 8000000))


def test_correct_documents_have_no_findings():
    data, proc, est, _ = control()
    assert service.verify(data, proc, est, RATES) == []


def test_detects_packs_rounded_down():
    data, proc, est, _ = control()
    floor = next(p for p in proc['payload']['purchases'] if p['offer_id'] == 'floor')
    floor.update(pack_count=9, purchased_quantity={'value': '19.8', 'unit': 'm2'}, amount_minor=1800000)
    next(x for x in est['payload']['lines'] if x['reference_id'] == floor['id'])['amount_minor'] = 1800000
    retotal(est)
    assert 'PACKS_ROUNDED_DOWN' in codes(data, proc, est)


def test_detects_repeated_delivery():
    data, proc, est, _ = control()
    extra = {**deepcopy(proc['payload']['deliveries'][0]), 'id': 'delivery-store-1-again'}
    proc['payload']['deliveries'].append(extra)
    est['payload']['lines'].append({'id': 'line-again', 'kind': 'delivery', 'reference_id': extra['id'], 'amount_minor': 250000, 'source_ids': extra['source_ids']})
    retotal(est)
    assert 'DUPLICATE_DELIVERY' in codes(data, proc, est)


def test_detects_reserve_lowered_without_rule_change():
    data, proc, est, sources = control()
    retotal(est, '5')
    assert est['payload']['total_minor'] == 7087500
    assert 'RESERVE_RULE' in codes(data, proc, est)
    # Общий валидатор проверяет только согласованность арифметики со ставкой из документа, поэтому подмену не видит.
    assembled = service.assemble_bundle(raw_example() | {'offers': data['offers']}, proc, est, sources)
    validate_bundle(assembled)


def test_detects_changed_line_amount_and_double_count():
    data, proc, est, _ = control()
    labor = next(x for x in est['payload']['lines'] if x['kind'] == 'labor')
    labor['amount_minor'] -= 100000
    est['payload']['lines'].append(deepcopy(labor))
    retotal(est)
    assert {'LINE_AMOUNT', 'DOUBLE_COUNT'} <= codes(data, proc, est)
