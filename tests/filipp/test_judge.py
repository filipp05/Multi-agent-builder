import json
import pytest
from evals.filipp.judge import build_payload, parse_verdict


def test_judge_does_not_receive_existing_verdict_or_internal_metadata():
    trial={'status':'passed','critical_count':0,'calls':[{'secret':'internal'}],
           'snapshots':[{'inputs':[{'text':'Покраска','fields':{}}],
                         'status':'waiting_input','draft':{'questions':[]},'error':None}]}
    payload=build_payload(trial,'Электрика блокируется.')
    data=json.loads(payload['messages'][1]['content'])
    assert 'calls' not in data and 'critical_count' not in data
    assert data['steps'][0]['inputs'][0]['text']=='Покраска'
    assert data['steps'][0]['status']=='waiting_input'
    assert payload['model']=='anthropic/claude-opus-5.5'


def test_judge_invalid_scores_are_not_accepted():
    with pytest.raises(ValueError):
        parse_verdict('{"scores":{"clarity":true},"issues":[],"uncertain":false}')


def test_judge_accepts_only_complete_rubric_and_records_fences():
    answer={'scores':{k:4 for k in ['clarity','faithfulness','explicit_limits','next_action']},
            'issues':[{'severity':'minor','evidence':'вопрос','comment':'неясный вопрос'}], 'uncertain':False}
    parsed=parse_verdict('```json\n'+json.dumps(answer)+'\n```')
    assert parsed['verdict']==answer
    assert parsed['fences_removed'] is True


def test_resume_keeps_completed_rows_and_old_failure(tmp_path,monkeypatch):
    import asyncio
    import sys
    from evals.filipp import judge
    items=[{'request':str(i),'source':str(i)} for i in range(3)]
    report=tmp_path/'judgments.json'
    report.write_text(json.dumps({'reports':[{'ok':True,**items[0],'receipt':'old'}],
                                  'failures':[{'ok':False,**items[1],'response':{'http_status':429}}]}))
    monkeypatch.setattr(judge,'prepare',lambda *args:items)
    called=[]
    verdict={'scores':{k:5 for k in judge.SCORES},'issues':[],'uncertain':False}
    receipt=tmp_path/'receipt.json';receipt.write_text(json.dumps({'content':json.dumps(verdict)}))
    def helper(*args,**kwargs):
        if args[0]=='status':return {'accounted_usd':1}
        called.append(args[args.index('--request')+1]);return {'ok':True,'result':str(receipt)}
    monkeypatch.setattr(judge,'helper',helper)
    monkeypatch.setattr(sys,'argv',['judge','--send','--sources',str(tmp_path),'--report-dir',str(tmp_path),'--concurrency','2'])
    with pytest.raises(SystemExit):asyncio.run(judge.main())
    saved=json.loads(report.read_text())
    assert called==['2']
    assert {x['request'] for x in saved['reports']}=={'0','2'}
    assert [x['request'] for x in saved['failures']]==['1']
