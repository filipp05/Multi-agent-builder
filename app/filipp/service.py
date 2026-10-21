from __future__ import annotations
import asyncio
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.filipp.brief import empty_draft,with_form,merge_extraction,prepare_review
from app.filipp.model import ModelRequest, ModelError
from app.filipp.repository import Conflict
from app.filipp.stages import build_confirmed_brief
from app.filipp.types import Extraction

BASE=Path(__file__).parent
ERRORS={'auth':'Войдите в Codex через ChatGPT и повторите.', 'quota':'Лимит подписки исчерпан. Повторите позже вручную.',
 'timeout':'Codex не ответил вовремя. Ввод сохранён.', 'execution':'Ошибка запуска Codex. Ввод сохранён.',
 'cli_missing':'Codex CLI не найден.', 'invalid_response':'Ответ модели не прошёл проверку. Ввод сохранён.',
 'output_limit':'Ответ Codex превысил допустимый размер.', 'temporary':'Временная ошибка после повторных попыток.'}

def new_input(text,fields=None,field_path=None):
    return {'id':str(uuid4()),'text':text,'fields':fields or {},'field_path':field_path,
            'received_at':datetime.now(ZoneInfo('Europe/Moscow')).isoformat()}

class Service:
    def __init__(self,repo,model):
        self.repo,self.model=repo,model
        self.tasks={}
        self.semaphore=asyncio.Semaphore(1)

    async def close(self):
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()),return_exceptions=True)
        await asyncio.to_thread(self.repo.recover_interrupted)

    def _enqueue(self,s):
        s['run_id']=str(uuid4());s['status']='queued';s['error']=None
        s['model_metadata']=None;s['state_version']+=1;s['input_version']=s['state_version']
        s['accepted_artifact_id']=None;s['artifact']=None
        return s

    def _launch(self,s):
        for rid,(pid,task) in list(getattr(self,'runs',{}).items()):
            if pid==s['project_id'] and rid!=s['run_id']: task.cancel()
        if not hasattr(self,'runs'): self.runs={}
        run=s['run_id']
        task=asyncio.create_task(self._work(s))
        self.tasks[run]=task;self.runs[run]=(s['project_id'],task)
        def done(_): self.tasks.pop(run,None);self.runs.pop(run,None)
        task.add_done_callback(done)

    async def create_project(self,input,action_id):
        body={'kind':'create','input':input.model_dump()}
        def create(_):
            item=new_input(input.text,input.fields)
            s={'project_id':str(uuid4()),'revision':1,'state_version':0,'run_id':None,
               'draft':with_form(empty_draft(),input.fields,item['id']), 'inputs':[item],
               'accepted_artifact_id':None,'artifact':None,'error':None}
            return self._enqueue(s)
        s,new=await asyncio.to_thread(self.repo.command,None,action_id,None,body,create)
        if new: self._launch(s)
        return s

    async def act(self,pid,kind,meta,extra=None):
        extra=extra or {}
        body={'kind':kind,'expected_version':meta.expected_version,**extra}
        def change(s):
            if kind=='confirm':
                if s['status']!='ready_to_confirm' or not s['draft']['ready']: raise Conflict('Сначала уточните требования')
                _,s['artifact']=build_confirmed_brief(s)
                s['accepted_artifact_id']=s['artifact']['artifact_id'];s['status']='confirmed';s['state_version']+=1
            elif kind=='cancel':
                if s['status'] not in ('queued','running'): raise Conflict('Нет активного вызова')
                s['status']='cancelled';s['state_version']+=1
            elif kind=='retry':
                if s['status'] not in ('failed','cancelled','interrupted'): raise Conflict('Повтор сейчас недоступен')
                self._enqueue(s)
            elif kind=='answers':
                if s['status']!='waiting_input': raise Conflict('Нет текущих вопросов')
                questions={q['id']:q for q in s['draft']['questions']}
                ids=[a['question_id'] for a in extra['answers']]
                if len(ids)!=len(set(ids)) or any(q not in questions for q in ids): raise Conflict('Устаревший или повторный вопрос')
                for answer in extra['answers']:
                    q=questions[answer['question_id']]
                    s['inputs'].append(new_input(answer['text'],field_path=q['field_path']))
                self._enqueue(s)
            elif kind=='edit':
                item=new_input(extra['input']['text'],extra['input']['fields'])
                item['is_edit']=True
                if s['status']=='confirmed': s['revision']+=1
                s['inputs'].append(item)
                # Preserve established requirements unless this edit explicitly changes them.
                s['draft']=with_form(s['draft'],item['fields'],item['id'])
                s['draft']['questions']=[]
                s['active_input_start']=len(s['inputs'])-1
                self._enqueue(s)
            else: raise ValueError('Неизвестная команда')
            return s
        s,new=await asyncio.to_thread(self.repo.command,pid,meta.action_id,meta.expected_version,body,change)
        if new and kind in ('answers','edit','retry'): self._launch(s)
        if new and kind=='cancel' and s['run_id'] in self.tasks: self.tasks[s['run_id']].cancel()
        return s

    async def _work(self,snapshot):
        pid,run,version=snapshot['project_id'],snapshot['run_id'],snapshot['input_version']
        async with self.semaphore:
            def running(s): s['status']='running';return s
            if not await asyncio.to_thread(self.repo.update_run,pid,run,version,running): return
            stage='model'
            metadata={}
            try:
                inputs=snapshot['inputs']
                schema=json.loads((BASE/'schemas/extraction.schema.json').read_text())
                schema['$defs']['FactCandidate']['properties']['input_id']['enum']=[item['id'] for item in inputs]
                reply=await self.model.generate(ModelRequest((BASE/'prompts/brief.txt').read_text(),
                    {'inputs':inputs,'current_values':snapshot['draft']['values'],'unresolved_conflicts':snapshot['draft']['conflicts'],'unsupported_requests':snapshot['draft']['unsupported_requests']},
                    schema))
                metadata=reply.metadata
                stage='extraction_validation'
                extraction=Extraction.model_validate(reply.data)
                stage='evidence_merge'
                draft=merge_extraction(snapshot['draft'],extraction,inputs)
                stage='review_validation'
                draft=prepare_review(draft)
                def success(s):
                    s['draft']=draft;s['model_metadata']=reply.metadata
                    s['status']='blocked_scope' if draft['unsupported_requests'] else ('ready_to_confirm' if draft['ready'] else 'waiting_input')
                    if s['status']=='blocked_scope': s['error']={'code':'unsupported_scope','message':'Эти работы пока не поддерживаем. Сейчас обрабатываем покраску стен и ламинат в одной прямоугольной сухой комнате. Исходный запрос сохранён.'}
                    return s
                await asyncio.to_thread(self.repo.update_run,pid,run,version,success)
            except asyncio.CancelledError: raise
            except Exception as e:
                code=e.code if isinstance(e,ModelError) else 'invalid_response'
                def failed(s):
                    s['model_metadata']=e.metadata if isinstance(e,ModelError) else {**metadata,'diagnostic':{'stage':stage,'exception_type':type(e).__name__}}
                    s['status']='failed';s['error']={'code':code,'message':ERRORS.get(code,ERRORS['invalid_response'])};return s
                await asyncio.to_thread(self.repo.update_run,pid,run,version,failed)
