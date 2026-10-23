import asyncio
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from app.brief.model import ModelReply, ModelRequest, ModelError
from app.design.stages import SOURCE, generate_design, output, work_plan_handler
from contracts.handoff import make_context, call_stage
from contracts.validate import validate_bundle
from app.procurement import service as oleg
from app.planning import service as karina
from app.providers.bedrock import BedrockModel

ROOT=Path(__file__).resolve().parents[2]

def seed():
    b=json.loads((ROOT/'contracts/examples/completed.json').read_text())
    b['artifacts']=b['artifacts'][:1]
    b['sources'].append(SOURCE)
    return b

class Model:
    def __init__(self):self.calls=[]
    async def generate(self,r):
        self.calls.append(r)
        props=r.schema['properties']
        if 'candidates' in props:
            data={'candidates':[],'ambiguities':[],'unsupported_requests':[]}
        elif 'concepts' in props:
            concepts=[]
            for i,k in enumerate(['calm','accent']):
                concepts.append({'id':k,'name':['Нейтральный','Контрастный'][i], 'description':'Покраска и ламинат',
                  'colors':[['#FFFFFF','#AAAAAA'],['#EEEEDD','#334455']][i], 'wall_finish':'paint','floor_finish':'laminate',
                  'hard_preferences_met':r.data['brief']['hard_preferences'],'source_ids':[SOURCE['id']]})
            data={'concepts':concepts}
        elif 'choices' in props:
            data={'choices':[{'requirement_id':k,'offer_id':v[0]['offer_id'],'reason':'Совместимо'} for k,v in r.data['candidates'].items()]}
        elif 'explanations' in props:
            data={'summary':'Проверено','explanations':[{'line_id':v['id'],'text':'Учебная цена'} for v in r.data['lines']]}
        elif 'task_explanations' in props:
            data={'summary':'Работы последовательно','calendar_notes':[],'task_explanations':[{'task_id':v['task_id'],'text':'По календарю'} for v in r.data['schedule']['items']]}
        elif 'findings' in props:data={'summary':'Проверено','findings':[]}
        else:raise AssertionError('Unexpected request')
        return ModelReply(data,{'provider':'fake-test'})

async def build(concept='calm',surface='prepared',budget=8000000):
    b=seed();b['artifacts'][0]['payload']['room']['surface_condition']=surface
    b['artifacts'][0]['payload']['budget_minor']=budget
    model=Model()
    _,d,_=await generate_design(model,make_context(b,'design_options',b['artifacts'],b['sources'],b['offers']))
    b['artifacts'].append(d)
    ctx=make_context(b,'design_selection',b['artifacts'],b['sources'],b['offers'],selected_concept_id=concept)
    _,d=call_stage(lambda c:output(c,{'concept_id':concept}),ctx);b['artifacts'].append(d)
    _,d=call_stage(work_plan_handler(),make_context(b,'work_plan',b['artifacts'],b['sources'],b['offers']));b['artifacts'].append(d)
    purchases=await oleg.run(b,model=model)
    b['sources']=purchases['sources'];b['artifacts'] += [purchases['procurement'],purchases['estimate']]
    out=await karina.run(b,model=model)
    return out,model

@pytest.mark.asyncio
async def test_full_contract_and_independent_audit():
    out,model=await build()
    assert out['bundle']['status']=='constraints_unresolved'
    validate_bundle(out['bundle'])
    assert out['audit']['payload']['status']=='incomplete'
    assert len(model.calls)==5

@pytest.mark.asyncio
async def test_selection_changes_packs_and_schedule():
    a,_=await build('calm');b,_=await build('accent')
    docs=lambda x:{a['kind']:a['payload'] for a in x['bundle']['artifacts']}
    x,y=docs(a),docs(b)
    assert x['work_plan']['requirements'][2]['waste_percent']=='10'
    assert y['work_plan']['requirements'][2]['waste_percent']=='15'
    assert x['procurement']['purchases']!=y['procurement']['purchases']
    assert x['schedule']['finish_at']!=y['schedule']['finish_at']

@pytest.mark.asyncio
@pytest.mark.parametrize('surface,budget',[('unknown',8000000),('needs_preparation',8000000),('prepared',1)])
async def test_unresolved_never_completed(surface,budget):
    out,_=await build(surface=surface,budget=budget)
    assert out['bundle']['status']=='constraints_unresolved'

@pytest.mark.asyncio
async def test_independent_audit_detects_tampered_quantity():
    out,_=await build();b=out['bundle']
    next(a for a in b['artifacts'] if a['kind']=='work_plan')['payload']['requirements'][1]['base_quantity']['value']='1'
    result=await karina.audit_existing(b)
    assert 'PAINT_QUANTITY' in {x['code'] for x in result['audit']['payload']['findings']}

@pytest.mark.asyncio
async def test_invalid_selection_rejected():
    b=seed()
    with pytest.raises(ValueError):make_context(b,'design_selection',b['artifacts'],b['sources'],b['offers'],selected_concept_id='absent')

@pytest.mark.asyncio
async def test_bedrock_schema_retry_usage_and_persistent_budget(tmp_path):
    replies=['bad-json','{"ok":true}'];count=0
    def handle(req):
        nonlocal count
        count+=1
        return httpx.Response(200,json={'stopReason':'end_turn','usage':{'inputTokens':10,'outputTokens':10},'output':{'message':{'content':[{'text':replies.pop(0)}]}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        model=BedrockModel(token='fake-test',ledger=tmp_path/'usage.db',client=client)
        r=await model.generate(ModelRequest('JSON',{}, {'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}))
        assert r.data=={'ok':True} and count==2
        assert r.metadata['usage']['inputTokens']==20
        again=BedrockModel(token='fake-test',ledger=tmp_path/'usage.db',budget_usd='0.000001',client=client)
        with pytest.raises(ModelError,match='quota'):await again.generate(ModelRequest('',{},{}))

@pytest.mark.asyncio
@pytest.mark.parametrize('status,code',[(403,'auth'),(429,'quota'),(503,'temporary')])
async def test_bedrock_errors_not_retried_or_leaked(tmp_path,status,code):
    calls=[]
    def handle(r):calls.append(r);return httpx.Response(status,text='SENSITIVE_CONTENT')
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        m=BedrockModel(token='fake-test',ledger=tmp_path/'u',client=client)
        with pytest.raises(ModelError) as err:await m.generate(ModelRequest('',{},{}))
        assert err.value.code==code and len(calls)==1
        assert 'SENSITIVE' not in str(err.value.metadata)

class Repo:
    def __init__(self):
        b=seed()['artifacts'][0];b['source_ids']=[]
        self.project={'project_id':b['project_id'],'revision':1,'status':'confirmed','state_version':2,'accepted_artifact_id':b['artifact_id'],'artifact':b}
    def get_project(self,pid):return deepcopy(self.project)

async def drain(p):
    while p.tasks:await asyncio.gather(*list(p.tasks.values()))

@pytest.mark.asyncio
async def test_pipeline_idempotency_restart_history_and_stale(tmp_path):
    from app.pipeline import Pipeline
    from app.brief.repository import Conflict
    repo=Repo();m=Model();p=Pipeline(repo,m,tmp_path/'p.db');pid=repo.project['project_id']
    first=await p.start(pid,'design',2);await drain(p)
    same=await p.start(pid,'design',2)
    assert same['run_id']==first['run_id'] and len(m.calls)==1
    selected=await p.start(pid,'pick',2,'calm',first['run_id']);await drain(p)
    assert p.current(pid)['status']=='constraints_unresolved'
    p2=Pipeline(repo,m,tmp_path/'p.db')
    assert p2.current(pid)['bundle']['status']=='constraints_unresolved'
    assert len(p2.store.history(pid))==2
    repo.project['status']='waiting_input'
    assert p2.current(pid)['stale']
    with pytest.raises(Conflict):await p2.start(pid,'new',2)

@pytest.mark.asyncio
async def test_stale_response_discarded(tmp_path):
    from app.pipeline import Pipeline
    repo=Repo();m=Model();real=m.generate
    async def slow(r):
        result=await real(r);repo.project['status']='waiting_input';return result
    m.generate=slow
    p=Pipeline(repo,m,tmp_path/'p');await p.start(repo.project['project_id'],'x',2);await drain(p)
    s=p.current(repo.project['project_id'])
    assert s['status']=='failed' and len(s['bundle']['artifacts'])==1

@pytest.mark.asyncio
async def test_cancel_and_recovery(tmp_path):
    from app.pipeline import Pipeline
    repo=Repo();m=Model()
    async def slow(r):await asyncio.sleep(30)
    m.generate=slow;p=Pipeline(repo,m,tmp_path/'p')
    await p.start(repo.project['project_id'],'x',2);await asyncio.sleep(0)
    await p.cancel(repo.project['project_id'])
    assert p.current(repo.project['project_id'])['status']=='interrupted'
    state=p.current(repo.project['project_id']);state['status']='audit';p.store.save(state)
    assert Pipeline(repo,m,tmp_path/'p').current(repo.project['project_id'])['status']=='interrupted'

def test_host_origin_and_mounts(tmp_path):
    from app.main import create_app
    app=create_app(data_dir=tmp_path,model=Model(),allowed_hosts=['testserver'])
    with TestClient(app) as c:
        assert c.get('/plan').status_code==200
        assert c.get('/oleg/').status_code==200
        assert c.get('/karina/').status_code==200
        assert c.get('/health',headers={'host':'evil.example'}).status_code==400
        assert c.post('/api/projects/x/plan',json={},headers={'origin':'https://evil.example'}).status_code==403


@pytest.mark.asyncio
@pytest.mark.parametrize('exclusion', ['Укладка ламината', 'Покраска стен', 'Неизвестное ограничение'])
async def test_excluded_work_blocks_new_pipeline_without_model_call(tmp_path, exclusion):
    from app.pipeline import Pipeline
    repo=Repo();repo.project['artifact']['payload']['excluded_work']=[exclusion]
    model=Model();pipeline=Pipeline(repo,model,tmp_path/'excluded.db')
    from app.brief.repository import Conflict
    with pytest.raises(Conflict,match='исключения'):
        await pipeline.start(repo.project['project_id'],'excluded',2)
    assert model.calls == []
    assert pipeline.current(repo.project['project_id']) is None


@pytest.mark.asyncio
async def test_excluded_work_blocks_direct_technology_and_imported_plan():
    out,_=await build()
    b=out['bundle']
    next(a for a in b['artifacts'] if a['kind']=='brief')['payload']['excluded_work']=['Укладка ламината']
    result,artifact=call_stage(work_plan_handler(),make_context(b,'work_plan',b['artifacts'],b['sources'],b['offers']))
    assert result['status']=='failed' and artifact is None
    audited=await karina.audit_existing(b)
    assert audited['audit']['payload']['status'] != 'passed'
    assert 'UNVERIFIED_EXCLUSIONS' in {f['code'] for f in audited['audit']['payload']['findings']}
