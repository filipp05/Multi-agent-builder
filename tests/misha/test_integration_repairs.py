"""Acceptance at the shared HTTP boundary, without live model calls."""
from copy import deepcopy
import time
import asyncio
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.oleg.service import example_bundle, example_choices
from test_mvp import Model
from contracts.validate import validate_bundle


def settled(client, url, active):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = client.get(url).json()
        if state and state['status'] not in active:
            return state
        time.sleep(.01)
    raise AssertionError('Сценарий не завершился')


def confirmed(client, budget, deadline='2026-10-30'):
    fields = {'room.length_m': '5', 'room.width_m': '4', 'room.height_m': '2.7',
              'room.openings_m2': '4', 'room.surface_condition': 'prepared',
              'budget_minor': budget, 'region': 'Москва', 'start_date': '2026-10-26',
              'deadline': deadline}
    first = client.post('/api/projects', json={'action_id': str(uuid4()),
                        'input': {'text': 'Покраска и ламинат', 'fields': fields}}).json()
    url = '/api/projects/' + first['project_id']
    draft = settled(client, url, {'running', 'queued'})
    assert draft['status'] == 'ready_to_confirm'
    return client.post(url + '/confirm', json={'action_id': str(uuid4()),
                       'expected_version': draft['state_version']}).json()


@pytest.mark.parametrize('budget,deadline,expected_budget', [
    (7000000, '2026-10-30', 'satisfied'),
    (1, '2026-10-30', 'violated'),
    (20000000, '2026-10-26', 'satisfied'),
])
def test_http_basket_revision_is_bounded_preserves_brief_and_history(tmp_path, budget, deadline, expected_budget):
    model = Model()
    with TestClient(create_app(data_dir=tmp_path, model=model)) as client:
        brief = confirmed(client, budget, deadline)
        url = '/api/projects/' + brief['project_id']
        command = {'action_id': str(uuid4()), 'expected_version': brief['state_version']}
        assert client.post(url + '/plan', json=command).status_code == 202
        design = settled(client, url + '/plan', {'designing'})
        command.update(action_id=str(uuid4()), expected_run=design['run_id'], concept_id='calm')
        assert client.post(url + '/plan', json=command).status_code == 202
        plan = settled(client, url + '/plan', {'planning', 'procurement', 'audit'})
        assert plan['status'] in ('completed', 'constraints_unresolved')
        attempts = plan['basket_attempts']
        assert 1 <= len(attempts) <= 3
        assert len({tuple(sorted(a['choices'].items())) for a in attempts}) == len(attempts)
        docs = {a['kind']: a['payload'] for a in plan['bundle']['artifacts']}
        assert docs['estimate']['budget_status'] == expected_budget
        assert docs['brief'] == brief['artifact']['payload']
        assert docs['design_selection']['concept_id'] == 'calm'
        request = next(r for r in model.calls if 'choices' in r.schema['properties'])
        assert request.data['selected_concept']['id'] == 'calm'
        assert 'catalogue_limitations' in request.data
        assert docs['audit']['status'] == 'incomplete'
        assert {'UNVERIFIED_FINISH', 'UNDISCLOSED_PREPARATION_KIT'} <= {
            f['code'] for f in docs['audit']['findings']}
        if budget == 7000000:
            assert len(attempts) > 1
            assert attempts[-1]['total_minor'] <= budget < attempts[0]['total_minor']
        if budget == 1 or deadline == '2026-10-26':
            assert plan['status'] == 'constraints_unresolved'
            assert 'нет другой корзины' in plan['revision_note']
        for attempt in attempts:
            validate_bundle(attempt['bundle'])
        assert client.get(url).json()['artifact'] == brief['artifact']
    with TestClient(create_app(data_dir=tmp_path, model=model)) as client:
        saved = client.get(url + '/plan').json()
        assert saved['basket_attempts'] == attempts
        assert saved['bundle'] == plan['bundle']


@pytest.mark.parametrize('route,key,value', [
    ('/oleg/api/run', 'artifacts', value) for value in ('invalid', [None], [1])
] + [
    (route, 'sources', {}) for route in ('/karina/api/run', '/karina/api/audit')
] + [
    ('/karina/api/audit', 'offers', value) for value in ('invalid', [None], [1])
])
def test_bad_documents_rejected_before_model_and_next_request_works(tmp_path, route, key, value):
    model = Model()
    with TestClient(create_app(data_dir=tmp_path, model=model)) as client:
        bundle = example_bundle()
        bundle[key] = deepcopy(value)
        response = client.post(route, json={'bundle': bundle})
        assert response.status_code == 422
        assert key in response.json()['detail']
        assert model.calls == []
        response = client.post('/oleg/api/run', json={
            'bundle': example_bundle(), 'choices': example_choices()})
        assert response.status_code == 200
        assert response.json()['estimate']['payload']['total_minor'] == 7425000
        assert model.calls == []


@pytest.mark.parametrize('action', ['cancel', 'edit'])
def test_revision_can_be_cancelled_or_invalidated_without_losing_previous_attempt(tmp_path, action):
    entered, release = Event(), Event()
    class SlowController(Model):
        reviews = 0
        async def generate(self, request):
            if 'findings' in request.schema['properties']:
                self.reviews += 1
                if self.reviews == 2:
                    entered.set()
                    while not release.is_set():
                        await asyncio.sleep(.01)
            return await super().generate(request)
    model = SlowController()
    with TestClient(create_app(data_dir=tmp_path, model=model)) as client:
        brief = confirmed(client, 7000000)
        url = '/api/projects/' + brief['project_id']
        client.post(url + '/plan', json={'action_id': str(uuid4()), 'expected_version': brief['state_version']})
        design = settled(client, url + '/plan', {'designing'})
        client.post(url + '/plan', json={'action_id': str(uuid4()), 'expected_version': brief['state_version'],
                    'concept_id': 'calm', 'expected_run': design['run_id']})
        assert entered.wait(30)
        original = client.get(url + '/plan').json()['basket_attempts'][0]
        if action == 'cancel':
            assert client.post(url + '/plan/cancel', json={}).status_code == 200
        else:
            response = client.post(url + '/edit', json={'action_id': str(uuid4()),
                'expected_version': brief['state_version'], 'input': {
                    'text': 'Меняю только бюджет', 'fields': {'budget_minor': 9000000}}})
            assert response.status_code == 202
        release.set()
        state = settled(client, url + '/plan', {'planning', 'procurement', 'audit'})
        assert state['status'] == ('interrupted' if action == 'cancel' else 'failed')
        assert state['basket_attempts'] == [original]
        assert len(original['bundle']['artifacts']) == 8
        if action == 'edit':
            assert state['stale']
