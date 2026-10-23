"""Reproducible Oleg → Karina checks; --live calls Codex, --review-filipp adds real HTTP brief checks."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.brief.model import CodexModel, ModelError
from app.planning import service as karina
from app.planning.audit import inspect
from app.procurement import service as oleg
from contracts.handoff import verify_handoffs
from contracts.validate import validate_bundle


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')


def payload(data, kind):
    return next(a['payload'] for a in data['artifacts'] if a['kind'] == kind)


class RecordedModel:
    def __init__(self, directory):
        self.real = CodexModel()
        self.directory = directory
        self.calls = []

    async def generate(self, request):
        props = request.schema.get('properties', {})
        role = 'planner' if 'calendar_notes' in props else ('brief' if 'candidates' in props else 'controller')
        try:
            reply = await self.real.generate(request)
            entry = {'role': role, 'status': 'ok', 'metadata': reply.metadata, 'response': reply.data}
        except ModelError as error:
            entry = {'role': role, 'status': 'failed', 'error': error.code, 'metadata': error.metadata}
            self.calls.append(entry)
            save(self.directory/'model-calls.json', self.calls)
            raise
        self.calls.append(entry)
        save(self.directory/'model-calls.json', self.calls)
        return reply


async def fresh_input(directory, name, *, deadline=None):
    data = oleg.example_bundle()
    run_id = str(uuid4())
    data['run_id'] = run_id
    for doc in data['artifacts']:
        doc['run_id'] = run_id
    if deadline:
        payload(data, 'brief')['deadline'] = deadline
    generated = await oleg.run(data, choices=oleg.example_choices())
    incoming = {**data, 'sources': generated['sources'],
                'artifacts': [a for a in data['artifacts'] if a['kind'] in oleg.UPSTREAM]
                             + [generated['procurement'], generated['estimate']]}
    save(directory/(name+'-oleg-output.json'), generated)
    save(directory/(name+'-input.json'), incoming)
    return incoming


def rebalance(estimate):
    base = sum(line['amount_minor'] for line in estimate['lines'])
    reserve = int((Decimal(base)/10).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    estimate.update(base_minor=base, reserve_minor=reserve, total_minor=base+reserve,
                    budget_status='satisfied' if base+reserve <= 8000000 else 'violated')


async def check_handoff(directory, model):
    incoming = await fresh_input(directory, 'reference')
    baseline = await karina.run(incoming)
    save(directory/'reference-program-only.json', baseline)
    cases = []

    async def trial(name, source, *, expected_code=None, audit_only=False, wishes=''):
        try:
            result = await (karina.audit_existing(source, model=model) if audit_only else
                            karina.run(source, model=model, wishes=wishes))
            save(directory/(name+'-result.json'), result)
            view = result['view']
            codes = {f['code'] for f in view['findings']}
            checks = {'fresh_oleg_total': payload(source, 'estimate')['total_minor'] == 7425000,
                      'expected_finish': view['finish_at'] == '2026-10-28T17:00:00+03:00'}
            if expected_code:
                checks.pop('fresh_oleg_total')
                checks['expected_finding'] = expected_code in codes
                checks['no_false_success'] = view['status'] == 'constraints_unresolved'
                if audit_only:
                    checks['failed_audit'] = view['audit_status'] == 'failed'
                    checks['schedule_preserved'] = payload(source, 'schedule') == payload(result['bundle'], 'schedule')
                else:
                    checks['deadline_violated'] = view['deadline_status'] == 'violated'
            else:
                independent = inspect(karina.audit_context(result['bundle']))
                checks['program_audit_honest'] = (independent['status'] == 'incomplete' and
                    {f['code'] for f in independent['findings']} ==
                    {'UNVERIFIED_FINISH', 'UNDISCLOSED_PREPARATION_KIT'})
                validate_bundle(result['bundle'])
                checks['eight_handoffs'] = len(verify_handoffs(result['bundle'])) == 8
            if model:
                checks['real_controller_response'] = bool(result['controller'] and result['controller']['summary'])
                if not audit_only:
                    checks['real_planner_response'] = bool(result['planner'] and result['planner']['summary'])
            row = {'name': name, 'passed': all(checks.values()), 'checks': checks,
                   'bundle_status': view['status'], 'audit_status': view['audit_status'],
                   'findings': view['findings'], 'model_log': result['model_log']}
        except Exception as error:
            row = {'name': name, 'passed': False, 'error': getattr(error, 'code', type(error).__name__),
                   'message': str(error), 'metadata': getattr(error, 'metadata', {})}
        cases.append(row)
        save(directory/'handoff-summary.json', cases)
        print(json.dumps({'case': name, 'passed': row['passed'], 'status': row.get('bundle_status'), 'error': row.get('error')}, ensure_ascii=False), flush=True)
        return row.get('error') not in ('auth', 'quota')

    if not await trial('reference', incoming, wishes='Объясни, как учтена сушка и почему успеваем до пятницы.'):
        return cases
    conflict = await fresh_input(directory, 'deadline-conflict', deadline='2026-10-26')
    if not await trial('deadline-conflict', conflict, expected_code='DEADLINE_EXCEEDED', wishes='Нужно закончить в понедельник 26 октября. Объясни конфликт.'):
        return cases
    for name, code in [('missing-line', 'MISSING_LINE'), ('duplicate-line', 'DUPLICATE_LINE')]:
        damaged = deepcopy(baseline['bundle'])
        est = payload(damaged, 'estimate')
        if name == 'missing-line':
            est['lines'].pop(0)
        else:
            est['lines'].append({**est['lines'][0], 'id': 'injected-duplicate-material'})
        rebalance(est)
        save(directory/(name+'-input.json'), damaged)
        if not await trial(name, damaged, expected_code=code, audit_only=True):
            break
    return cases


def check_filipp(directory, model):
    from fastapi.testclient import TestClient
    from app.brief.api import create_app
    db = directory/'filipp-review.sqlite3'
    app = create_app(db, model)
    snapshots = []
    checks = {}

    def settled(client, pid):
        deadline = time.monotonic()+240
        while time.monotonic() < deadline:
            state = client.get('/api/projects/'+pid).json()
            if state['status'] not in ('queued', 'running'):
                snapshots.append(state)
                save(directory/'filipp-snapshots.json', snapshots)
                return state
            time.sleep(.05)
        raise TimeoutError('Brief did not settle within 240 seconds')

    def command(client, state, action, **extra):
        return client.post(f'/api/projects/{state["project_id"]}/{action}', json={
            'action_id': str(uuid4()), 'expected_version': state['state_version'], **extra})

    try:
        with TestClient(app) as client:
            text = ('Покрасить стены и уложить ламинат в одной прямоугольной сухой комнате в Москве. '
                    'Длина 4 м, ширина 5 м, высота 2,7 м, общая площадь окон и дверей 4 м². Основание подготовлено. '
                    'Бюджет 80000 рублей. Начало 26 октября 2026, окончание до 30 октября 2026 включительно. '
                    'Обязательно без дорогого дерева. Хочется светло и легко мыть.')
            response = client.post('/api/projects', json={'action_id': str(uuid4()), 'input': {'text': text}})
            response.raise_for_status()
            state = settled(client, response.json()['project_id'])
            values = state['draft']['values']
            checks['facts_from_real_model'] = (values.get('budget_minor') == 8000000 and values.get('room.height_m') == '2.7'
                and {values.get('room.length_m'), values.get('room.width_m')} == {'4', '5'}
                and values.get('room.openings_m2') == '4' and values.get('start_date') == '2026-10-26'
                and values.get('deadline') == '2026-10-30')
            checks['explicit_confirmation_required'] = state['status'] == 'ready_to_confirm' and state['artifact'] is None
            if state['status'] != 'ready_to_confirm':
                raise ValueError('Full request did not reach ready_to_confirm')
            response = command(client, state, 'confirm')
            response.raise_for_status()
            confirmed = response.json()
            checks['confirmation_accepted'] = confirmed['status'] == 'confirmed'
            original = deepcopy(confirmed['artifact'])
            edit = command(client, confirmed, 'edit', input={'text': 'Меняю только бюджет на 60000 рублей. Остальные требования сохраняются.', 'fields': {'budget_minor': 6000000}})
            edit.raise_for_status()
            edited = settled(client, state['project_id'])
            checks['edit_preserves_geometry_and_revision'] = (edited['revision'] == 2 and edited['draft']['values']['budget_minor'] == 6000000
                and edited['draft']['values']['room.height_m'] == '2.7' and edited['status'] == 'ready_to_confirm')
            checks['stale_confirmation_rejected'] = command(client, confirmed, 'confirm').status_code == 409
            with app.state.repo.connect() as connection:
                old = json.loads(connection.execute('SELECT document FROM artifacts WHERE id=?', (original['artifact_id'],)).fetchone()[0])
            checks['prior_artifact_immutable'] = old == original
            checks['external_origin_rejected'] = client.post('/api/projects', headers={'origin': 'https://example.com'}, json={'action_id': str(uuid4()), 'input': {'text': text}}).status_code == 403

            # A second real request deliberately omits only the height from the form.
            fields = {'room.length_m': '4', 'room.width_m': '5', 'room.openings_m2': '4', 'budget_minor': 8000000,
                      'region': 'Москва', 'start_date': '2026-10-26', 'deadline': '2026-10-30'}
            response = client.post('/api/projects', json={'action_id': str(uuid4()), 'input': {'text': 'Покраска стен и ламинат.', 'fields': fields}})
            response.raise_for_status()
            missing = settled(client, response.json()['project_id'])
            questions = [q for q in missing['draft']['questions'] if q['field_path'] == 'room.height_m']
            checks['missing_height_question'] = missing['status'] == 'waiting_input' and bool(questions)
            checks['premature_confirmation_rejected'] = command(client, missing, 'confirm').status_code == 409
            if not questions:
                raise ValueError('Missing height was not requested')
            answered = command(client, missing, 'answers', answers=[{'question_id': questions[0]['id'], 'text': 'Высота комнаты 2,7 метра.'}])
            answered.raise_for_status()
            resolved = settled(client, missing['project_id'])
            checks['answer_resolves_height_and_keeps_budget'] = (resolved['status'] == 'ready_to_confirm'
                and resolved['draft']['values']['room.height_m'] == '2.7' and resolved['draft']['values']['budget_minor'] == 8000000)
        with TestClient(create_app(db, model)) as client:
            saved = client.get('/api/projects/'+edited['project_id']).json()
            checks['restart_preserves_project'] = saved['run_id'] == edited['run_id'] and saved['draft'] == edited['draft']
        row = {'name': 'filipp-live-http-review', 'actor': 'automated_scenario_driver', 'passed': all(checks.values()), 'checks': checks}
    except Exception as error:
        row = {'name': 'filipp-live-http-review', 'passed': False, 'checks': checks,
               'error': getattr(error, 'code', type(error).__name__), 'message': str(error)}
    save(directory/'filipp-review.json', row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


async def main(args):
    if args.review_filipp and not args.live:
        raise SystemExit('--review-filipp requires --live; no hidden model calls in the default run')
    directory = args.output_dir or ROOT/'.scratch/karina-mvp/results'/('handoff-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    directory.mkdir(parents=True, exist_ok=False)
    save(directory/'metadata.json', {'started_at': datetime.now(timezone.utc).isoformat(), 'live': args.live,
        'scope': 'Fresh Oleg handlers → Karina service; earlier design/work documents are labeled fixtures',
        'human_peer_review': False, 'actor': 'automated_scenario_driver'})
    print('Evidence:', directory, flush=True)
    model = RecordedModel(directory) if args.live else None
    cases = await check_handoff(directory, model)
    if args.review_filipp and not any(c.get('error') in ('auth', 'quota') for c in cases):
        cases.append(await asyncio.to_thread(check_filipp, directory, model))
    report = {'passed': len(cases) == (5 if args.review_filipp else 4) and all(c['passed'] for c in cases),
              'cases': cases, 'live_model_calls': len(model.calls) if model else 0, 'human_peer_review': False}
    save(directory/'summary.json', report)
    print('PASS' if report['passed'] else 'FAIL', directory/'summary.json', flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Six real planner/controller calls through the shared Codex adapter')
    parser.add_argument('--review-filipp', action='store_true', help='Add four real brief calls via public HTTP API and check lifecycle')
    parser.add_argument('--output-dir', type=Path, help='New evidence directory; existing receipts are never overwritten')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
