"""Приёмка API: заранее рассчитанные суммы, отказы, режим модели на явной тестовой замене."""
from fastapi.testclient import TestClient

from app.procurement.api import create_app
from oleg_helpers import EXAMPLE_CHOICES, FakeModel


def client(model=None):
    return TestClient(create_app(model or FakeModel()))


def example(c):
    data = c.get('/oleg/api/example').json()
    return {'bundle': data['bundle'], 'choices': data['choices']}


def test_page_and_example_are_served():
    c = client()
    page = c.get('/oleg/')
    assert page.status_code == 200 and 'Закупки и смета' in page.text
    assert "script-src 'self'" in page.headers['content-security-policy']
    data = c.get('/oleg/api/example').json()
    assert data['choices'] == EXAMPLE_CHOICES and 'не результат модели' in data['label']


def test_manual_choice_returns_precomputed_totals_without_model():
    model = FakeModel()
    c = client(model)
    r = c.post('/oleg/api/run', json=example(c))
    assert r.status_code == 200
    out = r.json()
    view = out['view']
    assert out['mode'] == 'manual' and model.requests == [] and out['explanations'] is None
    assert view['subtotals'] == {'material': 3000000, 'labor': 3500000, 'delivery': 250000}
    assert (view['base_minor'], view['reserve_minor'], view['total_minor']) == (6750000, 675000, 7425000)
    assert view['budget_minor'] == 8000000 and view['shortfall_minor'] == 0 and view['currency'] == 'RUB'
    assert view['room_check'] == {'wall_area_m2': '44.6', 'paint_task_m2': ['44.6']}
    assert all(line['sources'] for line in view['lines'])


def test_over_budget_shows_shortfall():
    c = client()
    body = example(c)
    next(a for a in body['bundle']['artifacts'] if a['kind'] == 'brief')['payload']['budget_minor'] = 7000000
    view = c.post('/oleg/api/run', json=body).json()['view']
    assert view['budget_status'] == 'violated' and view['shortfall_minor'] == 425000


def test_unknown_article_is_rejected():
    c = client()
    body = example(c)
    body['choices']['paint-req'] = 'paint-9000'
    r = c.post('/oleg/api/run', json=body)
    assert r.status_code == 422 and 'Неизвестный артикул' in r.json()['detail']
    assert r.json()['result']['error']['code'] == 'INVALID_INPUT'


def test_currency_mismatch_is_rejected():
    c = client()
    body = example(c)
    body['bundle']['offers'][1]['currency'] = 'USD'
    r = c.post('/oleg/api/run', json=body)
    assert r.status_code == 422 and 'Валюта' in r.json()['detail']


def test_unknown_delivery_reports_partial_not_total():
    c = client()
    body = example(c)
    body['choices']['paint-req'] = 'paint-s3'
    view = c.post('/oleg/api/run', json=body).json()['view']
    assert view['total_minor'] is None and view['budget_status'] == 'unknown' and view['shortfall_minor'] is None
    assert view['partial_known_minor'] == 3000000-600000+500000+3500000+250000


def test_model_selects_and_explains_but_code_counts():
    model = FakeModel({'prep-req': 'primer', 'paint-req': 'paint', 'floor-req': 'floor-s1-oak'})
    c = client(model)
    body = {**example(c), 'choices': None}
    r = c.post('/oleg/api/run', json=body)
    assert r.status_code == 200
    out = r.json()
    assert out['mode'] == 'model' and len(model.requests) == 2 and [x['agent'] for x in out['model_log']] == ['снабженец', 'сметчик']
    assert out['reasons']['floor-req'] == 'тест: floor-s1-oak'
    assert set(out['explanations']['lines']) == {x['id'] for x in out['estimate']['payload']['lines']}
    assert out['view']['total_minor'] == 6985000
    offered = {x['offer_id'] for x in model.requests[0].data['candidates']['floor-req']}
    assert 'floor-s1-wet' not in offered


def test_model_choice_outside_candidates_is_refused():
    c = client(FakeModel({**EXAMPLE_CHOICES, 'floor-req': 'floor-s1-wet'}))
    r = c.post('/oleg/api/run', json={**example(c), 'choices': None})
    assert r.status_code == 503 and r.json()['code'] == 'invalid_choice'


def test_incomplete_explanation_is_refused():
    c = client(FakeModel(explain_all=False))
    r = c.post('/oleg/api/run', json={**example(c), 'choices': None})
    assert r.status_code == 503 and r.json()['code'] == 'invalid_explanation'


def test_unavailable_model_is_reported_not_faked():
    c = client(FakeModel(error='cli_missing'))
    r = c.post('/oleg/api/run', json={**example(c), 'choices': None})
    assert r.status_code == 503 and 'cli_missing' in r.json()['detail']


def test_check_endpoint_flags_tampered_documents():
    c = client()
    body = example(c)
    out = c.post('/oleg/api/run', json=body).json()
    assert c.post('/oleg/api/check', json={'bundle': body['bundle'], 'procurement': out['procurement'], 'estimate': out['estimate']}).json() == {'ok': True, 'findings': []}
    out['estimate']['payload']['reserve_percent'] = '5'
    r = c.post('/oleg/api/check', json={'bundle': body['bundle'], 'procurement': out['procurement'], 'estimate': out['estimate']}).json()
    assert not r['ok'] and 'RESERVE_RULE' in {f['code'] for f in r['findings']}


def test_malformed_bundle_is_rejected_clearly():
    c = client()
    r = c.post('/oleg/api/run', json={'bundle': {'schema_version': '0.1.0'}, 'choices': EXAMPLE_CHOICES})
    assert r.status_code == 422 and 'нет поля' in r.json()['detail']
