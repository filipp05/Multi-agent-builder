import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from app.brief.model import CodexModel, ModelError, ModelRequest, run_process, safe_environment


def request():
    return ModelRequest(instruction='extract', data={}, schema={'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}, timeout_s=2)


@pytest.mark.asyncio
async def test_timeout_terminates_process(tmp_path):
    pidfile = tmp_path / 'pid'
    code = f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(20)'
    with pytest.raises(ModelError, match='timeout'):
        await run_process([sys.executable, '-c', code], timeout=.2)
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_cancel_rejects_late_output(tmp_path):
    pidfile = tmp_path / 'pid'
    task = asyncio.create_task(run_process([sys.executable, '-c', f'import os,time;open({str(pidfile)!r},"w").write(str(os.getpid()));time.sleep(20)'], timeout=10))
    for _ in range(100):
        if pidfile.exists(): break
        await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    with pytest.raises(ProcessLookupError): os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_output_limit():
    with pytest.raises(ModelError, match='output_limit'):
        await run_process([sys.executable, '-c', 'print("x"*10000)'], timeout=2, limit=100)


@pytest.mark.asyncio
@pytest.mark.parametrize('error,expected', [('401 Unauthorized','auth'), ('usage limit reached','quota'), ('nonsense','execution')])
async def test_auth_and_quota_do_not_retry(error, expected):
    calls=[]
    async def runner(argv, **kw):
        calls.append(argv)
        if 'login' in argv: return 0, b'Logged in using ChatGPT', b''
        return 1, b'', error.encode()
    with pytest.raises(ModelError, match=expected):
        await CodexModel(runner=runner).generate(request())
    assert len([a for a in calls if 'exec' in a]) == 1


@pytest.mark.asyncio
async def test_bad_json_is_not_success():
    async def runner(argv, **kw):
        if 'login' in argv: return 0,b'Logged in using ChatGPT',b''
        return 0,b'{"type":"item.completed","item":{"type":"agent_message","text":"wrong"}}\n',b''
    with pytest.raises(ModelError, match='invalid_response'):
        await CodexModel(runner=runner).generate(request())


@pytest.mark.asyncio
async def test_no_api_fallback(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'test-never-forward')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://example.invalid')
    assert 'OPENAI_API_KEY' not in safe_environment()
    assert 'OPENAI_BASE_URL' not in safe_environment()
    calls=[]
    async def runner(argv, **kw):
        calls.append(argv)
        return 0,b'Logged in using an API key',b''
    with pytest.raises(ModelError, match='auth'):
        await CodexModel(runner=runner).generate(request())
    assert all('exec' not in a for a in calls)


@pytest.mark.asyncio
async def test_real_json_protocol_and_fixed_security_config():
    calls=[]
    async def runner(argv, **kw):
        calls.append(argv)
        if 'login' in argv: return 0,b'Logged in using ChatGPT',b''
        return 0, (json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}})+'\n'+json.dumps({'type':'turn.completed','usage':{'input_tokens':1}})).encode(), b''
    result = await CodexModel(runner=runner).generate(request())
    assert result.data == {'ok': True}
    args=calls[-1]
    assert '--ignore-user-config' in args
    assert 'forced_login_method="chatgpt"' in args
    assert 'features.shell_tool=false' in args
    assert 'features.plugins=false' in args
    assert result.metadata['usage']['input_tokens'] == 1

@pytest.mark.asyncio
async def test_temporary_failure_records_all_attempts(monkeypatch):
    async def no_sleep(_): pass
    monkeypatch.setattr(asyncio,'sleep',no_sleep)
    count=0
    async def runner(argv,**kw):
        nonlocal count
        if 'login' in argv:return 0,b'Logged in using ChatGPT',b''
        count+=1
        if count<3:return 1,b'',b'503 Service Unavailable'
        return 0,b'{"type":"item.completed","item":{"type":"agent_message","text":"{\\"ok\\":true}"}}\n',b''
    result=await CodexModel(runner=runner).generate(request())
    assert len(result.metadata['attempts'])==3
    assert [x['error'] for x in result.metadata['attempts']]==['temporary','temporary',None]

def test_extraction_schema_rejects_unknown_field_paths():
    from app.brief.types import Extraction
    from jsonschema import Draft202012Validator
    schema=Extraction.model_json_schema()
    invalid={'candidates':[{'field_path':'calendar.deadline_date','value':'2026-12-01','input_id':'i','quote':'date'}], 'ambiguities':[], 'unsupported_requests':[]}
    assert list(Draft202012Validator(schema).iter_errors(invalid))
