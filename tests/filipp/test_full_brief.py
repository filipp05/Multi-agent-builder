import asyncio
from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient
from app.filipp.api import create_app
from app.filipp.model import ModelReply

FIELDS = {'room.length_m':'5','room.width_m':'4','room.height_m':'2.7','room.openings_m2':'3',
 'budget_minor':20000000,'region':'Москва','start_date':'2026-11-01','deadline':'2026-12-01'}

class EmptyModel:
    calls=0
    async def generate(self, request):
        self.calls+=1
        return ModelReply({'candidates':[], 'ambiguities':[], 'unsupported_requests':[]}, {'provider':'test','attempt':1})

def wait(client, pid):
    import time
    for _ in range(200):
        r=client.get('/api/projects/'+pid).json()
        if r['status'] not in ('queued','running'): return r
        time.sleep(.01)
    raise AssertionError('worker did not finish')

def create(client, fields=None, text='Покраска стен и ламинат'):
    r=client.post('/api/projects',json={'action_id':str(uuid4()),'input':{'text':text,'fields':FIELDS if fields is None else fields}})
    assert r.status_code==202, r.text
    return wait(client,r.json()['project_id'])

def command(client,s,kind,**kw):
    return client.post(f"/api/projects/{s['project_id']}/{kind}",json={'action_id':str(uuid4()),'expected_version':s['state_version'],**kw})

def test_full_request_requires_confirmation(tmp_path):
    model=EmptyModel()
    with TestClient(create_app(tmp_path/'db',model)) as c:
        s=create(c)
        assert s['status']=='ready_to_confirm'
        assert s['accepted_artifact_id'] is None
        r=command(c,s,'confirm')
        assert r.status_code==200,r.text
        assert r.json()['status']=='confirmed'
        assert r.json()['artifact']['payload']['budget_minor']==20000000
        assert r.json()['artifact']['payload']['room']['height_m']=='2.7'
        assert model.calls==1
        assert c.get('/api/projects/'+s['project_id']).json()['artifact']==r.json()['artifact']


def test_incomplete_cannot_confirm(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        s=create(c,{'budget_minor':20000000})
        assert s['status']=='waiting_input'
        assert command(c,s,'confirm').status_code==409

def test_bad_numeric_form_returns_422(tmp_path):
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        r=c.post('/api/projects',json={'action_id':'bad','input':{'text':'Покраска','fields':{'budget_minor':'abc'}}})
        assert r.status_code==422
