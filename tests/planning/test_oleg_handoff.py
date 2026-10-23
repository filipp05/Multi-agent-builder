"""Integration: freshly generated procurement/estimate, never the fixture's saved outputs."""
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP

import pytest

from app.procurement import service as oleg
from app.planning import service as karina
from app.planning.audit import inspect
from contracts.handoff import verify_handoffs
from contracts.validate import validate_bundle
from karina_helpers import FakeModel, payload


async def fresh_bundle(*, deadline=None, choices=None):
    data = oleg.example_bundle()
    if deadline:
        payload(data, 'brief')['deadline'] = deadline
    generated = await oleg.run(data, choices=choices or oleg.example_choices())
    incoming = {**data, 'sources': generated['sources'],
                'artifacts': [a for a in data['artifacts'] if a['kind'] in oleg.UPSTREAM]
                             + [generated['procurement'], generated['estimate']]}
    return incoming, generated


@pytest.mark.asyncio
async def test_fresh_oleg_results_pass_karina_and_all_shared_handoffs():
    incoming, generated = await fresh_bundle()
    original = deepcopy(incoming)
    result = await karina.run(incoming, model=FakeModel())
    assert incoming == original
    assert generated['estimate']['payload']['total_minor'] == 7425000
    assert [p['pack_count'] for p in generated['procurement']['payload']['purchases']] == [1, 2, 10]
    assert result['view']['finish_at'] == '2026-10-28T17:00:00+03:00'
    assert result['view']['status'] == 'completed' and result['view']['findings'] == []
    schedule = result['schedule']
    audit = result['audit']
    assert generated['procurement']['artifact_id'] in schedule['input_artifact_ids']
    assert {generated['procurement']['artifact_id'], generated['estimate']['artifact_id']} <= set(audit['input_artifact_ids'])
    assert generated['procurement']['artifact_id'] != 'a-procurement'
    assert generated['estimate']['artifact_id'] != 'a-estimate'
    assert len(verify_handoffs(result['bundle'])) == 8


def test_fresh_documents_cross_both_public_http_apis():
    from fastapi.testclient import TestClient
    from app.procurement.api import create_app as oleg_app
    from app.planning.api import create_app as karina_app
    model = FakeModel()
    data = oleg.example_bundle()
    with TestClient(oleg_app(model)) as client:
        response = client.post('/oleg/api/run', json={'bundle': data, 'choices': oleg.example_choices()})
        assert response.status_code == 200, response.text
        generated = response.json()
    assert model.requests == []
    incoming = {**data, 'sources': generated['sources'],
                'artifacts': [a for a in data['artifacts'] if a['kind'] in oleg.UPSTREAM]
                             + [generated['procurement'], generated['estimate']]}
    with TestClient(karina_app(model)) as client:
        response = client.post('/karina/api/run', json={'bundle': incoming, 'use_model': True})
        assert response.status_code == 200, response.text
        result = response.json()
    assert payload(result['bundle'], 'estimate')['total_minor'] == 7425000
    assert result['bundle']['status'] == 'completed'
    assert len(model.requests) == 2
    assert len(verify_handoffs(result['bundle'])) == 8


@pytest.mark.asyncio
async def test_fresh_handoff_without_semantic_model_stays_incomplete():
    incoming, _ = await fresh_bundle()
    result = await karina.run(incoming)
    assert result['audit']['payload']['status'] == 'incomplete'
    assert result['bundle']['status'] == 'constraints_unresolved'
    assert inspect(karina.audit_context(result['bundle'])) == {'status': 'passed', 'findings': []}
    validate_bundle(result['bundle'])


@pytest.mark.asyncio
async def test_deadline_conflict_preserves_correct_dates_and_honest_status():
    incoming, generated = await fresh_bundle(deadline='2026-10-26')
    result = await karina.run(incoming, model=FakeModel())
    assert generated['estimate']['payload']['total_minor'] == 7425000
    assert result['view']['finish_at'] == '2026-10-28T17:00:00+03:00'
    assert result['view']['deadline_status'] == 'violated'
    assert result['bundle']['status'] == 'constraints_unresolved'
    assert 'DEADLINE_EXCEEDED' in {f['code'] for f in result['view']['findings']}
    validate_bundle(result['bundle'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fault, expected', [('missing', 'MISSING_LINE'), ('duplicate', 'DUPLICATE_LINE')])
async def test_tampered_fresh_estimate_is_detected_even_after_arithmetic_is_rebalanced(fault, expected):
    incoming, _ = await fresh_bundle()
    initial = await karina.run(incoming)
    damaged = deepcopy(initial['bundle'])
    est = payload(damaged, 'estimate')
    if fault == 'missing':
        est['lines'].pop(0)
    else:
        est['lines'].append({**est['lines'][0], 'id': 'duplicate-material-entry'})
    # Keep totals internally consistent: the auditor must reconstruct expected coverage.
    base = sum(line['amount_minor'] for line in est['lines'])
    reserve = int((Decimal(base)/10).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    est.update(base_minor=base, reserve_minor=reserve, total_minor=base+reserve,
               budget_status='satisfied' if base+reserve <= 8000000 else 'violated')
    schedule = deepcopy(payload(damaged, 'schedule'))
    result = await karina.audit_existing(damaged, model=FakeModel())
    assert result['audit']['payload']['status'] == 'failed'
    assert result['bundle']['status'] == 'constraints_unresolved'
    assert expected in {f['code'] for f in result['view']['findings']}
    assert payload(result['bundle'], 'schedule') == schedule


@pytest.mark.asyncio
@pytest.mark.parametrize('choices, total, status, deadline_status', [
    ({'prep-req': 'primer', 'paint-req': 'paint', 'floor-req': 'floor-s1-oak'}, 6985000, 'satisfied', 'satisfied'),
    ({'prep-req': 'primer-s2', 'paint-req': 'paint-s2', 'floor-req': 'floor-s2'}, 7271000, 'satisfied', 'violated'),
    ({'prep-req': 'primer', 'paint-req': 'paint-s3', 'floor-req': 'floor'}, None, 'unknown', 'unknown')])
async def test_alternative_fresh_baskets_and_unknown_delivery(choices, total, status, deadline_status):
    incoming, generated = await fresh_bundle(choices=choices)
    result = await karina.run(incoming, model=FakeModel())
    assert generated['estimate']['payload']['total_minor'] == total
    assert generated['estimate']['payload']['budget_status'] == status
    assert result['view']['deadline_status'] == deadline_status
    if status == 'unknown':
        assert result['audit']['payload']['status'] == 'incomplete'
        assert result['bundle']['status'] == 'constraints_unresolved'
        assert {'UNKNOWN_DELIVERY_COST', 'UNKNOWN_DELIVERY_DATE'} <= {f['code'] for f in result['view']['findings']}
    elif deadline_status == 'violated':
        assert result['audit']['payload']['status'] == 'passed'
        assert result['bundle']['status'] == 'constraints_unresolved'
        assert result['view']['finish_at'] == '2026-11-02T17:00:00+03:00'
        assert {f['code'] for f in result['view']['findings']} == {'DEADLINE_EXCEEDED'}
        validate_bundle(result['bundle'])
    else:
        assert result['view']['findings'] == []
        assert result['bundle']['status'] == 'completed'
        validate_bundle(result['bundle'])
