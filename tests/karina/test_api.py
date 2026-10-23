from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.filipp.model import ModelError, ModelReply
from app.karina.api import configure, create_app
from contracts.handoff import verify_handoffs
from contracts.validate import validate_bundle
from karina_helpers import FakeModel, bundle, payload


def client(model=None):
    return TestClient(create_app(model or FakeModel()))


def test_page_example_static_and_cross_origin_protection():
    c = client()
    page = c.get('/karina/')
    assert page.status_code == 200 and 'Независимая проверка' in page.text
    assert "script-src 'self'" in page.headers['content-security-policy']
    assert c.get('/karina/static/app.js').status_code == 200
    assert 'не генерируются моделью' in c.get('/karina/api/example').json()['label']
    assert c.post('/karina/api/run', json={'bundle': bundle()}, headers={'origin': 'https://example.com'}).status_code == 403


def test_router_mounts_into_a_shared_app():
    app = FastAPI()
    configure(app, FakeModel())
    c = TestClient(app)
    assert c.get('/karina/').status_code == 200
    assert c.post('/karina/api/run', json={'bundle': bundle()}).status_code == 200


def test_program_only_path_is_honest_and_does_not_call_model():
    model = FakeModel()
    data = client(model).post('/karina/api/run', json={'bundle': bundle()}).json()
    assert model.requests == [] and data['mode'] == 'checks_only'
    assert data['view']['audit_status'] == 'incomplete'
    assert data['bundle']['status'] == 'constraints_unresolved'
    validate_bundle(data['bundle'])


def test_model_path_returns_a_complete_bundle_and_two_separate_contexts():
    model = FakeModel()
    response = client(model).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True, 'calendar_wishes': 'Объясните срок'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['view']['status'] == 'constraints_unresolved' and data['audit']['payload']['status'] == 'incomplete'
    assert len(model.requests) == 2
    assert len(verify_handoffs(data['bundle'])) == 8
    assert model.requests[0].data['calendar_wishes'] == 'Объясните срок'
    controller = model.requests[1].data
    assert {a['kind'] for a in controller['artifacts']} == {'brief', 'design_options', 'design_selection', 'work_plan', 'procurement', 'estimate', 'schedule'}
    assert 'planner' not in controller and 'audit' not in controller


def test_budget_or_deadline_conflict_cannot_become_completed():
    data = bundle()
    payload(data, 'brief').update(budget_minor=7000000, deadline='2026-10-26')
    payload(data, 'estimate')['budget_status'] = 'violated'
    response = client().post('/karina/api/run', json={'bundle': data, 'use_model': True})
    result = response.json()
    assert result['view']['status'] == 'constraints_unresolved'
    assert result['view']['deadline_status'] == 'violated'
    assert {'BUDGET_EXCEEDED', 'DEADLINE_EXCEEDED'} <= {f['code'] for f in result['view']['findings']}


def test_existing_schedule_is_audited_without_silently_repairing_it():
    data = bundle()
    payload(data, 'procurement')['deliveries'][0]['available_on'] = '2026-11-10'
    existing = deepcopy(payload(data, 'schedule'))
    response = client().post('/karina/api/audit', json={'bundle': data})
    result = response.json()
    assert response.status_code == 200
    assert result['view']['audit_status'] == 'failed'
    assert 'BEFORE_DELIVERY' in {f['code'] for f in result['view']['findings']}
    assert payload(result['bundle'], 'schedule') == existing


def test_model_cannot_erase_program_findings_or_accept_foreign_references():
    data = bundle()
    payload(data, 'estimate')['lines'].pop()
    result = client().post('/karina/api/audit', json={'bundle': data, 'use_model': True}).json()
    assert result['view']['audit_status'] == 'failed'
    assert 'MISSING_LINE' in {f['code'] for f in result['view']['findings']}
    model = FakeModel([{'severity': 'blocking', 'code': 'MADE_UP', 'message': 'Некорректное замечание',
                        'artifact_ids': ['foreign'], 'item_ids': [], 'owner': 'karina', 'source_ids': []}])
    rejected = client(model).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True})
    assert rejected.status_code == 503 and rejected.json()['code'] == 'invalid_response'


def test_semantic_insufficiency_is_incomplete_and_valid_findings_are_retained():
    model = FakeModel([{'severity': 'warning', 'code': 'DATA_INCOMPLETE', 'message': 'Нужно уточнить условие пользователя.',
                        'artifact_ids': ['a-brief'], 'item_ids': [], 'owner': 'filipp', 'source_ids': []}])
    result = client(model).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True}).json()
    assert result['view']['audit_status'] == 'incomplete' and result['view']['status'] == 'constraints_unresolved'
    assert 'LLM_DATA_INCOMPLETE' in {f['code'] for f in result['view']['findings']}


def test_controller_can_reference_existing_catalogue_items():
    # Real live-review regression: catalogue IDs are not row IDs inside an artifact.
    model = FakeModel([{'severity': 'warning', 'code': 'DATA_INCOMPLETE',
                        'message': 'Подтвердите цвет и возможность мытья выбранных покрытий.',
                        'artifact_ids': ['a-procurement'], 'item_ids': ['paint', 'floor'],
                        'owner': 'oleg', 'source_ids': ['demo-prices']}])
    response = client(model).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True})
    assert response.status_code == 200, response.text
    result = response.json()
    finding = next(f for f in result['view']['findings'] if f['code'] == 'LLM_DATA_INCOMPLETE')
    assert finding['item_ids'] == ['paint', 'floor']
    assert result['view']['audit_status'] == 'incomplete'


def test_controller_cannot_reference_nonexistent_catalogue_items():
    model = FakeModel([{'severity': 'warning', 'code': 'DATA_INCOMPLETE', 'message': 'Недопустимая ссылка.',
                        'artifact_ids': ['a-procurement'], 'item_ids': ['not-in-the-catalogue'],
                        'owner': 'oleg', 'source_ids': []}])
    response = client(model).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True})
    assert response.status_code == 503 and response.json()['code'] == 'invalid_response'


def test_failed_model_is_not_replaced_by_success():
    class Unavailable:
        async def generate(self, request):
            raise ModelError('auth')
    response = client(Unavailable()).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True})
    assert response.status_code == 503 and response.json()['code'] == 'auth'


def test_bad_planner_task_references_are_rejected():
    class BadPlanner(FakeModel):
        async def generate(self, request):
            reply = await super().generate(request)
            reply.data['task_explanations'].pop()
            return reply
    response = client(BadPlanner()).post('/karina/api/run', json={'bundle': bundle(), 'use_model': True})
    assert response.status_code == 503


def test_cycle_and_malformed_requests_produce_clear_failure():
    c = client()
    data = bundle()
    payload(data, 'work_plan')['tasks'][0]['predecessor_ids'] = ['floor-task']
    response = c.post('/karina/api/run', json={'bundle': data})
    assert response.status_code == 422
    assert response.json()['result']['error']['code'] == 'INVALID_INPUT'
    assert 'Цикл' in response.json()['detail']
    assert c.post('/karina/api/run', json={'bundle': {}}).status_code == 422
    assert c.post('/karina/api/run', json={'bundle': bundle(), 'extra': 'value'}).status_code == 422


def test_stale_artifact_is_an_addressed_audit_error():
    data = bundle()
    data['artifacts'][0]['run_id'] = 'old-run'
    response = client().post('/karina/api/audit', json={'bundle': data})
    assert response.status_code == 200
    assert 'STALE_REVISION' in {f['code'] for f in response.json()['view']['findings']}


def test_midnight_shift_end_passes_the_shared_integration_validator():
    data = bundle()
    payload(data, 'brief')['calendar']['day_start_hour'] = 16
    response = client().post('/karina/api/run', json={'bundle': data, 'use_model': True})
    result = response.json()
    assert result['view']['status'] == 'constraints_unresolved'
    assert result['view']['finish_at'] == '2026-10-29T00:00:00+03:00'
    validate_bundle(result['bundle'])
