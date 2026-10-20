import json
from pathlib import Path

import pytest
from contracts.handoff import make_context, call_stage, verify_handoffs


def bundle():
    return json.loads((Path(__file__).resolve().parents[1]/'contracts/examples/completed.json').read_text())


@pytest.mark.parametrize('filename', ['completed.json', 'over-budget.json'])
def test_all_handoffs(filename):
    data = json.loads((Path(__file__).resolve().parents[1]/'contracts/examples'/filename).read_text())
    assert len(verify_handoffs(data)) == 8


def test_work_plan_can_resolve_selected_concept_without_future_outputs():
    d = bundle()
    context = make_context(d, 'work_plan', d['artifacts'], d['sources'], d['offers'])
    assert {a['kind'] for a in context['input_artifacts']} == {'brief', 'design_selection'}
    assert {a['kind'] for a in context['referenced_artifacts']} == {'design_options'}
    context['offers'].clear()
    assert d['offers']


def test_missing_dependency_is_rejected():
    d = bundle()
    with pytest.raises(ValueError, match='не готовы'):
        make_context(d, 'estimate', [], d['sources'], d['offers'])


def test_stale_input_is_rejected():
    d = bundle()
    next(a for a in d['artifacts'] if a['kind']=='brief')['revision'] += 1
    with pytest.raises(ValueError, match='другому запуску'):
        make_context(d, 'design_options', d['artifacts'], d['sources'], d['offers'])


def test_invalid_user_selection_is_rejected():
    d = bundle()
    with pytest.raises(ValueError, match='выбор'):
        make_context(d, 'design_selection', d['artifacts'], d['sources'], d['offers'], selected_concept_id='missing')


def test_handler_cannot_change_revision_to_accept_stale_output():
    d = bundle()
    context = make_context(d, 'brief', [], d['sources'], d['offers'], user_request='Ремонт комнаты')
    def handler(incoming):
        incoming['revision'] += 1
        result = {k: incoming[k] for k in ('schema_version','project_id','run_id','revision')}
        result.update(stage='brief', status='needs_input', artifact_id=None,
                      questions=[{'field_path':'room', 'message':'Какие размеры?'}], error=None)
        return result, None
    with pytest.raises(ValueError, match='другому запуску'):
        call_stage(handler, context)
    assert context['revision'] == 1
