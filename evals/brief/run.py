"""Explicit live evaluation through the production service. Never imported by pytest."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from app.brief.model import CodexModel,ModelError
from app.brief.repository import Repository
from app.brief.service import Service
from app.brief.types import UserInput,CommandMeta
from app.brief.presentation import present
from evals.brief.parallel import parallel_map
from evals.brief.acceptance import check_gate
from evals.brief.metrics import summarize,compare_facts,fact_count

ROOT=Path(__file__).resolve().parents[2]
DATA=Path(__file__).parent/'data'

def fingerprint():
    paths=sorted([* (ROOT/'app/filipp').rglob('*.py'),*(ROOT/'app/brief/prompts').glob('*'),*(ROOT/'app/brief/schemas').glob('*'),*(ROOT/'evals/filipp').rglob('*.py'),*DATA.glob('*.jsonl'), ROOT/'evals/brief/manifest.json', *(ROOT/'app/brief/static').glob('*'), *(ROOT/'app/brief/templates').glob('*')])
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}

def freeze_metadata(path,metadata):
    if path.exists():
        old=json.loads(path.read_text())
        comparable=lambda m:{k:v for k,v in m.items() if k!='started_at'}
        if comparable(old)!=comparable(metadata):
            raise ValueError('Код/набор/параметры изменились: используйте новое имя запуска.')
        return old
    path.write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
    return metadata


class RecordedModel:
    def __init__(self): self.calls=[];self.real=CodexModel()
    async def generate(self,request):
        start=time.monotonic()
        try:
            reply=await self.real.generate(request)
            self.calls.append({'status':'ok','response':reply.data,'metadata':reply.metadata})
            return reply
        except ModelError as e:
            self.calls.append({'status':'failed','error':e.code,'metadata':e.metadata,'duration_s':time.monotonic()-start})
            raise

async def wait(repo,pid):
    while True:
        s=await asyncio.to_thread(repo.get_project,pid)
        if s['status'] not in ('queued','running'): return s
        await asyncio.sleep(.05)

def should_stop(row, *, newly_executed):
    return newly_executed and any(c.get('error') in ('quota','auth') for c in row['calls'])


def count_exec_attempts(calls):
    return sum(len(c.get('metadata',{}).get('attempts',[])) for c in calls)


def meta(s):return CommandMeta(action_id=str(uuid4()),expected_version=s['state_version'])

async def scenario(case,repeat,directory):
    start=time.monotonic(); model=RecordedModel()
    repo=Repository(directory/(case['id']+'-'+str(repeat)+'.sqlite3'))
    service=Service(repo,model)
    snapshots=[];defects=[];critical=[];qc=0;edit_submitted=False
    try:
        s=await service.create_project(UserInput.model_validate(case['input']),str(uuid4()))
        s=await wait(repo,s['project_id']);snapshots.append(s)
        actual_q={q['field_path'] for q in s['draft']['questions']}
        expected_q=set(case['first_questions'])
        qc=len(actual_q & expected_q)
        if expected_q-actual_q:defects.append('Не заданы вопросы: '+','.join(sorted(expected_q-actual_q)))
        for field in expected_q:
            # Existing conflict values are allowed; fabricated missing fields are not.
            if field not in case['input']['fields'] and field in s['draft']['values'] and field not in s['draft']['conflicts']:
                critical.append('Выдумано отсутствующее поле '+field)
        if s['status']=='waiting_input' and case['answers']:
            answers=[{'question_id':q['id'],'text':case['answers'][q['field_path']]} for q in s['draft']['questions'] if q['field_path'] in case['answers']]
            if answers:
                s=await service.act(s['project_id'],'answers',meta(s),{'answers':answers})
                s=await wait(repo,s['project_id']);snapshots.append(s)
        if case['edit'] and s['status']=='ready_to_confirm':
            s=await service.act(s['project_id'],'confirm',meta(s));snapshots.append(s)
            s=await service.act(s['project_id'],'edit',meta(s),{'input':case['edit']})
            edit_submitted=True
            s=await wait(repo,s['project_id']);snapshots.append(s)
        values=s['draft']['values'];expected=case['expected_initial'] if case['edit'] and not edit_submitted else case['expected_final']
        correct=compare_facts(values,expected,s['draft']['origins'])['correct']
        for key,value in expected.items():
            if values.get(key)!=value:
                defects.append('Неверное/пропущенное поле '+key)
                if key in values and key in ('budget_minor','room.length_m','room.width_m','room.height_m','room.openings_m2','hard_preferences','start_date','deadline'):
                    critical.append('Изменён критический факт '+key)
        if case['expected_outcome']=='rejected_scope':
            if s['status'] in ('ready_to_confirm','confirmed') or s.get('artifact'):critical.append('Неподдерживаемая работа пропущена')
            if s['status']!='blocked_scope' or not s['draft']['unsupported_requests'] or not s.get('error'):defects.append('Неверный отказ')
            status='passed' if not defects and not critical else 'failed'
            correct=0;expected={}
        else:
            if s['status']=='ready_to_confirm':
                s=await service.act(s['project_id'],'confirm',meta(s));snapshots.append(s)
            if s['status']!='confirmed':defects.append('Конечное состояние '+s['status'])
            status='passed' if not defects and not critical else 'failed'
        compared=compare_facts(values,expected,s['draft']['origins']) if case['expected_outcome']!='rejected_scope' else {'extracted':0,'unexpected':[]}
        extracted=compared['extracted'];unexpected=compared['unexpected']
        if unexpected: defects.append('Непредусмотренные факты: '+','.join(unexpected));status='failed'
        return {'trace_version':2,'policy_version':2,'actor':'scenario_driver','events':repo.list_events(s['project_id']),
            'views':[present(snapshot) for snapshot in snapshots], 'expected_outcome':case['expected_outcome'],
            'expected_values':expected,'edit_submitted':edit_submitted,'actual_state':s['status'],'scenario_passed':status=='passed','scenario_id':case['id'],'repeat':repeat,'status':status,'critical_count':len(critical),'critical':critical,
            'defects':defects,'correct':correct,'expected':fact_count(expected),'extracted':extracted,
            'questions_correct':qc,'questions_expected':len(case['first_questions']),
            'adapter_calls':len(model.calls),'model_calls':count_exec_attempts(model.calls),'attempts':count_exec_attempts(model.calls),'user_wait_s':0,
            'duration_s':time.monotonic()-start,'calls':model.calls,'snapshots':snapshots}
    finally: await service.close()

async def main(args):
    manifest=json.loads((Path(__file__).parent/'manifest.json').read_text())
    if args.split=='holdout' and (not manifest['thresholds'] or not manifest['thresholds_approved']):
        raise SystemExit('Holdout заблокирован: сначала согласуйте и зафиксируйте пороги на dev.')
    cases=[json.loads(x) for x in (DATA/(args.split+'.jsonl')).read_text().splitlines()]
    if args.limit:cases=cases[:args.limit]
    directory=ROOT/'.scratch/filipp-mvp/results'/args.name
    directory.mkdir(parents=True,exist_ok=True)
    frozen=fingerprint()
    import subprocess,shutil
    cli=subprocess.run(['codex','--version'],capture_output=True,text=True,timeout=10).stdout.strip()
    for name in frozen:
        target=directory/'source'/name;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copyfile(ROOT/name,target)
    runmeta={'started_at':datetime.now(timezone.utc).isoformat(),'split':args.split,'repeats':args.repeats,'concurrency':args.concurrency,'limit':args.limit,
             'fingerprints':frozen,'manifest':manifest,'model':'gpt-6-astra','reasoning_effort':'low','cli':cli}
    meta_path=directory/'metadata.json'
    freeze_metadata(meta_path,runmeta)
    rows=[];errors=[];started=time.monotonic()
    sessions_path=directory/'sessions.json'
    sessions=json.loads(sessions_path.read_text()) if sessions_path.exists() else []
    session={'started_at':datetime.now(timezone.utc).isoformat(),'completed':False,'wall_time_s':0}
    sessions.append(session)
    async def trial(item):
        case,repeat=item
        path=directory/(case['id']+'-'+str(repeat)+'.json')
        newly_executed=not path.exists()
        if path.exists():row=json.loads(path.read_text())
        else:
            row=await scenario(case,repeat,directory)
            path.write_text(json.dumps(row,ensure_ascii=False,indent=2))
        return row,newly_executed
    jobs=[(case,repeat) for case in cases for repeat in range(1,args.repeats+1)]
    async for item,result,error in parallel_map(jobs,trial,args.concurrency,
            halt=lambda result:should_stop(result[0],newly_executed=result[1]),
            throttle=lambda result:any(c.get('error')=='rate_limit' for c in result[0]['calls'])):
        if error:
            errors.append(type(error).__name__);continue
        row,newly_executed=result;rows.append(row)
        summary=summarize(rows)
        summary.update(wall_time_s=time.monotonic()-started,concurrency=args.concurrency,
                       gate=check_gate(rows,manifest['thresholds'],expected_runs=len(jobs)))
        session['wall_time_s']=time.monotonic()-started
        sessions_path.write_text(json.dumps(sessions,ensure_ascii=False,indent=2))
        summary['wall_time_s']=sum(x['wall_time_s'] for x in sessions)
        (directory/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        print(json.dumps({k:row[k] for k in ('scenario_id','repeat','status','critical_count','duration_s')},ensure_ascii=False),flush=True)
    session['completed']=not errors and len(rows)==len(jobs)
    session['wall_time_s']=time.monotonic()-started
    sessions_path.write_text(json.dumps(sessions,ensure_ascii=False,indent=2))
    if errors or len(rows)!=len(jobs):
        print(json.dumps({'incomplete':True,'errors':errors,'completed':len(rows)}),flush=True);return 2
    (directory/'SUMMARY.md').write_text('# Измерение '+args.split+'\n\n'+json.dumps(summarize(rows),ensure_ascii=False,indent=2)+'\n\nКорректные отказы считаются отдельно от подтверждённых требований. Набор известный, регрессионный.\n')
    print(json.dumps(summarize(rows),ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--split',choices=['dev','holdout'],required=True)
    parser.add_argument('--repeats',type=int,default=3,choices=[1,3]);parser.add_argument('--limit',type=int)
    parser.add_argument('--name',default='dev-v1');parser.add_argument('--concurrency',type=int,default=3)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
