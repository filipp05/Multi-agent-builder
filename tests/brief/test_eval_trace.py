import json
from fastapi.testclient import TestClient
from app.brief.api import create_app
from evals.brief.judge import build_payload
from test_full_brief import EmptyModel,create,command


def test_confirm_event_is_real_and_idempotent(tmp_path):
    app=create_app(tmp_path/'db',EmptyModel())
    with TestClient(app) as c:
        s=create(c)
        assert not any(x['kind']=='confirm' for x in app.state.repo.list_events(s['project_id']))
        body={'action_id':'same','expected_version':s['state_version']}
        for _ in range(2):assert c.post('/api/projects/'+s['project_id']+'/confirm',json=body).status_code==200
        events=app.state.repo.list_events(s['project_id'])
        assert sum(x['kind']=='confirm' for x in events)==1


def test_judge_uses_views_and_events_not_inferred_clicks():
    s={'status':'confirmed','inputs':[],'draft':{},'artifact':None}
    old=json.loads(build_payload({'snapshots':[s]},'policy')['messages'][1]['content'])
    assert old['trace_complete'] is False
    assert old['events']==[]
    view={**s,'review':[{'display_value':'200 000,00 ₽'}],'warnings':['Электрику пока не можем реализовать'],'next_action':'Проверьте'}
    new=json.loads(build_payload({'snapshots':[s],'views':[view],'events':[{'kind':'confirm','version':4}],'actor':'scenario_driver'},'policy')['messages'][1]['content'])
    assert new['trace_complete'] is True
    assert new['events'][0]['kind']=='confirm'
    assert new['steps'][0]['warnings']==view['warnings']
    assert new['actor']=='scenario_driver'


def test_scope_trial_does_not_accept_auth_failure_as_refusal(tmp_path,monkeypatch):
    import asyncio
    from evals.brief import run
    from app.brief.model import ModelError
    class Fails:
        def __init__(self):self.calls=[]
        async def generate(self,request):raise ModelError('auth')
    monkeypatch.setattr(run,'RecordedModel',Fails)
    case=json.loads(next(x for x in (run.DATA/'dev.jsonl').read_text().splitlines() if '"d14"' in x))
    row=asyncio.run(run.scenario(case,1,tmp_path))
    assert row['status']=='failed'
    assert row['actual_state']=='failed'
    assert row['scenario_passed'] is False


def test_run_metadata_cannot_change_parameters_on_resume(tmp_path):
    import pytest
    from evals.brief.run import freeze_metadata
    path=tmp_path/'metadata.json'
    first={'started_at':'first','fingerprints':{},'concurrency':3,'split':'dev'}
    freeze_metadata(path,first)
    freeze_metadata(path,{**first,'started_at':'later'})
    assert json.loads(path.read_text())['started_at']=='first'
    with pytest.raises(ValueError):freeze_metadata(path,{**first,'concurrency':6})


def test_judge_gets_only_visible_questions_for_complete_trace():
    snapshot={'status':'blocked_scope','draft':{'questions':[{'message':'hidden question'}]},'inputs':[],'artifact':None}
    data=json.loads(build_payload({'snapshots':[snapshot],'views':[snapshot],'events':[],'actor':'scenario_driver'},'policy')['messages'][1]['content'])
    assert 'draft' not in data['steps'][0]
    assert data['steps'][0]['visible_questions']==[]
    snapshot['status']='waiting_input'
    data=json.loads(build_payload({'snapshots':[snapshot],'views':[snapshot],'events':[]},'policy')['messages'][1]['content'])
    assert data['steps'][0]['visible_questions']==[{'message':'hidden question'}]
