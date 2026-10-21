import asyncio
from copy import deepcopy
from uuid import uuid4
from fastapi.testclient import TestClient
from app.filipp.api import create_app
from app.filipp.model import ModelError
from test_full_brief import EmptyModel,FIELDS,create,wait,command

class Slow(EmptyModel):
    async def generate(self,request):
        await asyncio.sleep(30)
        return await super().generate(request)


def test_duplicate_action_starts_one_run(tmp_path):
    m=EmptyModel()
    with TestClient(create_app(tmp_path/'db',m)) as c:
        body={'action_id':'one','input':{'text':'Покраска','fields':FIELDS}}
        a=c.post('/api/projects',json=body).json()
        b=c.post('/api/projects',json=body).json()
        assert a['run_id']==b['run_id']
        wait(c,a['project_id'])
        assert m.calls==1
        body['input']['text']='Другое'
        assert c.post('/api/projects',json=body).status_code==409

def test_stale_confirm_conflicts(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        s=create(c)
        r=command(c,s,'edit',input={'text':'Новый бюджет','fields':{**FIELDS,'budget_minor':25000000}})
        assert r.status_code==202
        assert command(c,s,'confirm').status_code==409
        assert wait(c,s['project_id'])['draft']['values']['budget_minor']==25000000

def test_cancel_rejects_late_result(tmp_path):
    app=create_app(tmp_path/'db',Slow())
    with TestClient(app) as c:
        s=c.post('/api/projects',json={'action_id':'create','input':{'text':'Покраска','fields':FIELDS}}).json()
        s=c.get('/api/projects/'+s['project_id']).json()
        # Worker may transition queued→running before the cancel reaches it. Retry stale version once.
        r=command(c,s,'cancel')
        if r.status_code==409: r=command(c,c.get('/api/projects/'+s['project_id']).json(),'cancel')
        assert r.status_code==200
        assert not app.state.repo.update_run(s['project_id'],s['run_id'],s['input_version'],lambda x:{**x,'status':'confirmed'})
        assert c.get('/api/projects/'+s['project_id']).json()['status']=='cancelled'

def test_edit_rejects_old_model_result(tmp_path):
    app=create_app(tmp_path/'db',EmptyModel())
    with TestClient(app) as c:
        old=create(c)
        command(c,old,'edit',input={'text':'Новое','fields':FIELDS})
        assert not app.state.repo.update_run(old['project_id'],old['run_id'],old['input_version'],lambda x:x)

def test_restart_marks_interrupted_and_retry_keeps_inputs(tmp_path):
    db=tmp_path/'db'
    with TestClient(create_app(db,Slow())) as c:
        s=c.post('/api/projects',json={'action_id':'one','input':{'text':'Покраска','fields':FIELDS}}).json()
    with TestClient(create_app(db,EmptyModel())) as c:
        now=c.get('/api/projects/'+s['project_id']).json()
        assert now['status']=='interrupted'
        r=command(c,now,'retry');assert r.status_code==202
        done=wait(c,s['project_id'])
        assert done['run_id']!=s['run_id']
        assert done['inputs']==s['inputs']
        assert done['status']=='ready_to_confirm'

def test_changed_payload_same_key_conflicts(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        s=create(c)
        body={'action_id':'confirm','expected_version':s['state_version']}
        url=f"/api/projects/{s['project_id']}/confirm"
        a=c.post(url,json=body);b=c.post(url,json=body)
        assert a.status_code==b.status_code==200
        assert a.json()['artifact']==b.json()['artifact']
        body['expected_version']+=1
        assert c.post(url,json=body).status_code==409

def test_previous_confirmed_artifact_survives_edit(tmp_path):
    app=create_app(tmp_path/'db',EmptyModel())
    with TestClient(app) as c:
        s=command(c,create(c),'confirm').json()
        old_id=s['accepted_artifact_id']
        command(c,s,'edit',input={'text':'Меняем','fields':FIELDS})
        new=wait(c,s['project_id'])
        assert new['revision']==2
        with app.state.repo.connect() as db:
            assert db.execute('SELECT id FROM artifacts WHERE id=?',(old_id,)).fetchone()

def test_external_origin_cannot_start_model(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        assert c.post('/api/projects',headers={'Origin':'https://example.com'},json={'action_id':'x','input':{'text':'a'}}).status_code==403

def test_edit_budget_preserves_existing_constraints(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        s=create(c,{**FIELDS,'hard_preferences':['Без запаха']})
        r=command(c,s,'edit',input={'text':'Меняю только бюджет','fields':{'budget_minor':25000000}})
        assert r.status_code==202
        done=wait(c,s['project_id'])
        assert done['draft']['values']['hard_preferences']==['Без запаха']
        assert done['draft']['values']['room.height_m']=='2.7'

def test_unrelated_edit_cannot_clear_budget_conflict(tmp_path):
    from test_clarifications import ConflictModel
    class Once(ConflictModel):
        async def generate(self,request):
            if not getattr(self,'seen',False):
                self.seen=True
                return await super().generate(request)
            return await EmptyModel.generate(self,request)
    with TestClient(create_app(tmp_path/'db',Once())) as c:
        s=create(c,text='Бюджет 100000 рублей')
        assert s['status']=='waiting_input'
        command(c,s,'edit',input={'text':'Меняю регион','fields':{'region':'Тула'}})
        now=wait(c,s['project_id'])
        assert 'budget_minor' in now['draft']['conflicts']
        assert command(c,now,'confirm').status_code==409


def test_unrelated_edit_cannot_clear_open_scope_question(tmp_path):
    from app.filipp.model import ModelReply
    class Once(EmptyModel):
        async def generate(self,request):
            if not getattr(self,'seen',False):
                self.seen=True
                return ModelReply({'candidates':[],'ambiguities':[],'unsupported_requests':['сантехника']},{})
            return await super().generate(request)
    with TestClient(create_app(tmp_path/'db',Once())) as c:
        s=create(c,text='Сантехника и покраска')
        assert s['status']=='blocked_scope'
        command(c,s,'edit',input={'text':'Меняю регион','fields':{'region':'Тула'}})
        now=wait(c,s['project_id'])
        assert now['status']=='blocked_scope'
        assert command(c,now,'confirm').status_code==409


def test_edit_during_running_keeps_unprocessed_text(tmp_path):
    from app.filipp.model import ModelReply
    class Model(EmptyModel):
        async def generate(self,request):
            self.calls+=1
            if self.calls==1: await asyncio.sleep(30)
            first=next((x for x in request.data['inputs'] if 'Без запаха' in x['text']),None)
            facts=[] if first is None else [{'field_path':'hard_preferences','value':'["Без запаха"]','input_id':first['id'],'quote':'Без запаха'}]
            return ModelReply({'candidates':facts,'ambiguities':[],'unsupported_requests':[]},{})
    app=create_app(tmp_path/'db',Model())
    with TestClient(app) as c:
        s=c.post('/api/projects',json={'action_id':'a','input':{'text':'Покраска. Без запаха','fields':FIELDS}}).json()
        import time
        for _ in range(100):
            s=c.get('/api/projects/'+s['project_id']).json()
            if s['status']=='running': break
            time.sleep(.01)
        command(c,s,'edit',input={'text':'Меняю регион','fields':{'region':'Тула'}})
        now=wait(c,s['project_id'])
        assert now['draft']['values']['hard_preferences']==['Без запаха']

def test_late_adapter_response_after_cancel_is_discarded(tmp_path):
    from app.filipp.model import ModelReply
    class IgnoresCancel(EmptyModel):
        async def generate(self,request):
            try: await asyncio.sleep(30)
            except asyncio.CancelledError: pass
            self.calls+=1
            return ModelReply({'candidates':[],'ambiguities':[],'unsupported_requests':[]},{})
    model=IgnoresCancel()
    with TestClient(create_app(tmp_path/'db',model)) as c:
        s=c.post('/api/projects',json={'action_id':'a','input':{'text':'Покраска','fields':FIELDS}}).json()
        import time
        for _ in range(100):
            s=c.get('/api/projects/'+s['project_id']).json()
            if s['status']=='running': break
            time.sleep(.01)
        assert command(c,s,'cancel').status_code==200
        for _ in range(100):
            if model.calls:break
            time.sleep(.01)
        assert model.calls==1
        final=c.get('/api/projects/'+s['project_id']).json()
        assert final['status']=='cancelled'
        assert final['artifact'] is None
