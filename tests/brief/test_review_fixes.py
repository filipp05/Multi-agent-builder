import json
from copy import deepcopy
import pytest
from app.brief.brief import empty_draft,with_form,merge_extraction,prepare_review
from app.brief.types import Extraction
from test_full_brief import FIELDS

def extraction(path,values):
    return Extraction.model_validate({'candidates':[{'field_path':path,'value':v,'input_id':'edit','quote':'quoted'} for v in values],'ambiguities':[],'unsupported_requests':[]})

def test_edit_form_text_budget_conflict_remains_question():
    fields={**FIELDS,'budget_minor':12000000}
    source={'id':'edit','text':'quoted','fields':fields,'is_edit':True}
    draft=prepare_review(merge_extraction(with_form(empty_draft(),fields,'edit'),extraction('budget_minor',['10000000']),[source]))
    assert draft['values']['budget_minor']==12000000
    assert draft['conflicts']['budget_minor']['candidate']==10000000
    assert not draft['ready']

def test_edit_preserves_all_separate_preference_candidates_and_old_constraints():
    d=with_form(empty_draft(),{'hard_preferences':['Светлый пол']},'old')
    source={'id':'edit','text':'quoted','fields':{},'is_edit':True}
    result=merge_extraction(d,extraction('hard_preferences',['["Без запаха"]','["Без пыли"]']),[source])
    assert result['values']['hard_preferences']==['Светлый пол','Без запаха','Без пыли']

def test_explicit_list_answer_replaces_old_list_but_keeps_all_candidates():
    d=with_form(empty_draft(),{'hard_preferences':['old']},'old')
    source={'id':'edit','text':'quoted','fields':{},'field_path':'hard_preferences'}
    result=merge_extraction(d,extraction('hard_preferences',['["a"]','["b"]']),[source])
    assert result['values']['hard_preferences']==['a','b']

def test_cleared_list_in_form_is_not_silently_repopulated_from_same_text():
    source={'id':'edit','text':'quoted','fields':{'hard_preferences':[]},'is_edit':True}
    d=with_form(empty_draft(),source['fields'],'edit')
    result=merge_extraction(d,extraction('hard_preferences',['["old"]']),[source])
    assert result['values']['hard_preferences']==[]
    assert 'hard_preferences' in result['conflicts']

def test_judge_missing_helper_has_actionable_error(monkeypatch,tmp_path):
    from evals.brief import judge
    monkeypatch.setenv('ELIZA_HELPER',str(tmp_path/'absent.py'))
    with pytest.raises(RuntimeError,match='ELIZA_HELPER'):judge.helper('status')

def test_judge_uses_configured_helper(monkeypatch,tmp_path):
    from evals.brief import judge
    script=tmp_path/'helper.py';script.write_text('print(\'{"ok":true}\')')
    monkeypatch.setenv('ELIZA_HELPER',str(script))
    assert judge.helper('status')=={'ok':True}

@pytest.mark.asyncio
async def test_unreached_edit_is_failure_without_false_changed_fact(tmp_path,monkeypatch):
    from evals.brief import run
    from app.brief.model import ModelError
    class Fails:
        def __init__(self):self.calls=[]
        async def generate(self,request):raise ModelError('invalid_response')
    monkeypatch.setattr(run,'RecordedModel',Fails)
    case=json.loads(next(x for x in (run.DATA/'dev.jsonl').read_text().splitlines() if '"d17"' in x))
    case['expected_initial']=case['input']['fields']
    row=await run.scenario(case,1,tmp_path)
    assert row['status']=='failed'
    assert row['critical_count']==0
    assert row['expected_values']['room.width_m']=='4'

@pytest.mark.parametrize('can_read,exit_code',[(False,0),(True,1)])
def test_isolation_probe_creates_dirs_and_returns_outcome(tmp_path,monkeypatch,can_read,exit_code):
    import asyncio
    from scripts import brief_isolation_probe as probe
    from app.brief.model import ModelReply
    monkeypatch.chdir(tmp_path)
    async def generate(self,request):
        from pathlib import Path
        return ModelReply({'could_read':can_read,'file_content':Path(request.data['path']).read_text() if can_read else ''},{})
    monkeypatch.setattr(probe.CodexModel,'generate',generate)
    assert asyncio.run(probe.main())==exit_code
    assert (tmp_path/'.scratch/filipp-mvp/results/isolation.json').is_file()
    assert not (tmp_path/'.scratch/filipp-mvp/runtime/isolation-canary.txt').exists()

def test_empty_initial_preferences_form_does_not_conflict_with_text():
    source={'id':'edit','text':'quoted','fields':{'hard_preferences':[]}}
    d=with_form(empty_draft(),source['fields'],'edit')
    result=merge_extraction(d,extraction('hard_preferences',['["Без запаха"]']),[source])
    assert result['values']['hard_preferences']==['Без запаха']
    assert result['conflicts']=={}

@pytest.mark.asyncio
async def test_judge_cli_dry_run_reports_missing_dependency(monkeypatch,tmp_path,capsys):
    import sys
    from evals.brief import judge
    monkeypatch.setenv('ELIZA_HELPER',str(tmp_path/'absent.py'))
    monkeypatch.setattr(judge,'prepare',lambda *a:[{'request':'unused','source':'unused'}])
    monkeypatch.setattr(sys,'argv',['judge','--sources',str(tmp_path),'--report-dir',str(tmp_path)])
    with pytest.raises(SystemExit) as exc:await judge.main()
    assert exc.value.code==2
    assert 'ELIZA_HELPER' in capsys.readouterr().out
