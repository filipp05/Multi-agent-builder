import json
import pytest
from app.filipp.model import CodexModel, ModelError, ModelRequest
from app.filipp.brief import format_field_value

SCHEMA={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
def message(text):
    return json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}).encode()

@pytest.mark.parametrize('raw,stage',[
    (b'not-json-secret','event_json'), (b'[]','event_shape'),
    (b'{"type":"turn.failed","error":{"message":"secret"}}','cli_event'),
    (b'{"type":"turn.completed"}','missing_message'),
    (message('secret'),'message_json'), (message('{"ok":"secret"}'),'schema'),
])
def test_parse_failures_are_distinguishable_without_content(raw,stage):
    with pytest.raises(ModelError) as exc: CodexModel._parse(raw,SCHEMA)
    assert exc.value.code=='invalid_response'
    diagnostic=exc.value.metadata['diagnostic']
    assert diagnostic['stage']==stage
    assert len(diagnostic['stdout_sha256'])==64
    assert 'secret' not in json.dumps(diagnostic)

@pytest.mark.asyncio
async def test_generate_preserves_diagnostic_without_retry():
    calls=[]
    async def runner(argv,**kwargs):
        if 'login' in argv:return 0,b'Logged in using ChatGPT',b''
        calls.append(argv)
        return 0,message('{"ok":"secret"}'),b''
    with pytest.raises(ModelError) as exc:
        await CodexModel(runner=runner).generate(ModelRequest('extract',{},SCHEMA))
    assert exc.value.metadata['diagnostic']['stage']=='schema'
    assert len(calls)==1

@pytest.mark.parametrize('value,expected',[(9,'09:00'),(0,'00:00'),(23,'23:00'),(30,'30 — неверное время')])
def test_workday_time_format(value,expected):
    assert format_field_value('calendar.day_start_hour',value)==expected

from app.filipp.brief import empty_draft
from app.filipp.presentation import present

@pytest.mark.parametrize('code,action',[('invalid_response','Повторить'),('auth','Войдите'),('quota','позже')])
def test_failed_view_explains_saved_unprocessed_input(code,action):
    view=present({'status':'failed','draft':empty_draft(),'error':{'code':code}})
    assert 'сохранён' in view['next_action']
    assert 'не завершена' in view['next_action']
    assert action in view['next_action']

def test_review_labels_match_display_dates_and_weekdays():
    draft=empty_draft()
    draft['values'].update(start_date='2026-11-01',deadline='2026-12-01')
    view=present({'status':'ready_to_confirm','draft':draft})
    rows={row['field']:row for row in view['review']}
    assert 'ГГГГ' not in rows['start_date']['label']
    assert 'включительно' in rows['deadline']['label']
    assert '0' not in rows['calendar.work_weekdays']['label']

@pytest.mark.parametrize('wrap,stage',[(lambda s:s.encode(),'event_json'),(message,'message_json')])
def test_deep_json_retains_parse_diagnostic(wrap,stage):
    raw=wrap('['*10000+'0'+']'*10000)
    with pytest.raises(ModelError) as exc:CodexModel._parse(raw,SCHEMA)
    assert exc.value.metadata['diagnostic']['stage']==stage
    assert exc.value.metadata['diagnostic']['exception_type']=='RecursionError'

RECONNECT={'type':'error','message':'Reconnecting... 2/5 (unexpected status 403 Forbidden: Unknown error)'}
COMPLETE={'type':'turn.completed','usage':{'input_tokens':1}}
def stream(*events):
    return b'\n'.join(e if isinstance(e,bytes) else json.dumps(e).encode() for e in events)

@pytest.mark.asyncio
async def test_recovered_cli_stream_is_validated_without_new_exec():
    calls=[]
    async def runner(argv,**kwargs):
        if 'login' in argv:return 0,b'Logged in using ChatGPT',b''
        calls.append(argv)
        return 0,stream(RECONNECT,RECONNECT,message('{"ok":true}'),COMPLETE),b''
    reply=await CodexModel(runner=runner).generate(ModelRequest('extract',{},SCHEMA))
    assert reply.data=={'ok':True}
    assert len(calls)==1
    assert reply.metadata['attempt']==1

@pytest.mark.parametrize('raw',[
 stream(RECONNECT,message('{"ok":true}')),
 stream(message('{"ok":true}'),RECONNECT,COMPLETE),
 stream(RECONNECT,message('{"ok":true}'),{'type':'turn.failed'}),
 stream(RECONNECT,message('{"ok":true}'),COMPLETE,RECONNECT),
 stream({'type':'error','message':'Unknown fatal error'},message('{"ok":true}'),COMPLETE),
])
def test_unrecovered_or_terminal_error_remains_failure(raw):
    with pytest.raises(ModelError) as exc:CodexModel._parse(raw,SCHEMA)
    assert exc.value.metadata['diagnostic']['stage']=='cli_event'

@pytest.mark.parametrize('text,stage',[('wrong','message_json'),('{"ok":"wrong"}','schema')])
def test_recovery_does_not_bypass_payload_validation(text,stage):
    with pytest.raises(ModelError) as exc:
        CodexModel._parse(stream(RECONNECT,message(text),COMPLETE),SCHEMA)
    assert exc.value.metadata['diagnostic']['stage']==stage

def test_source_schema_is_scoped_to_each_dialogue(tmp_path):
    from fastapi.testclient import TestClient
    from app.filipp.api import create_app
    from test_full_brief import create
    from test_clarifications import AnswerModel
    from jsonschema import Draft202012Validator
    class Capture(AnswerModel):
        requests=[]
        async def generate(self,request):
            self.requests.append(request)
            return await super().generate(request)
    model=Capture()
    with TestClient(create_app(tmp_path/'db',model)) as client:
        create(client)
        create(client)
    first,second=model.requests
    def data(source):
        return {'candidates':[{'field_path':'region','value':'Тула','quote':'Тула','input_id':source}],'ambiguities':[],'unsupported_requests':[]}
    first_id=first.data['inputs'][0]['id']
    second_id=second.data['inputs'][0]['id']
    assert not list(Draft202012Validator(first.schema).iter_errors(data(first_id)))
    assert list(Draft202012Validator(first.schema).iter_errors(data('invented-source')))
    assert list(Draft202012Validator(first.schema).iter_errors(data(second_id)))
    assert not list(Draft202012Validator(second.schema).iter_errors(data(second_id)))

def test_evidence_failure_retains_safe_diagnostic_and_blocks_confirmation(tmp_path):
    from fastapi.testclient import TestClient
    from app.filipp.api import create_app
    from app.filipp.model import ModelReply
    from test_full_brief import create,command
    class WrongSource:
        async def generate(self,request):
            return ModelReply({'candidates':[{'field_path':'region','value':'Тула','quote':'secret','input_id':'wrong'}],'ambiguities':[],'unsupported_requests':[]},{'provider':'test'})
    with TestClient(create_app(tmp_path/'db',WrongSource())) as client:
        state=create(client)
        assert state['status']=='failed'
        assert command(client,state,'confirm').status_code==409
        assert state.get('artifact') is None
        assert state['model_metadata']['diagnostic']['stage']=='evidence_merge'
        assert 'secret' not in json.dumps(state['model_metadata'])
