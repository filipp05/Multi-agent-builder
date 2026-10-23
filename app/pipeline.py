"""Single-process coordinator; durable runs, explicit concept selection, bounded work."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.brief.repository import Conflict, NotFound
from app.brief.model import ModelError
from app.design.scope import unsupported_exclusions, SCOPE_MESSAGE
from app.design.stages import SOURCE, generate_design, work_plan_handler, output
from app.procurement import service as procurement
from app.procurement.stages import load
from app.planning import service as planning
from contracts.handoff import IDENTITY, make_context, call_stage
from contracts.validate import validate_bundle
from app.basket_revision import alternative, limits

ACTIVE = {'designing', 'planning', 'procurement', 'audit'}
ERRORS = {'unsupported_scope':SCOPE_MESSAGE, 'auth':'Провайдер не принял доступ к модели. Проверьте авторизацию и срок действия ключа.',
          'quota':'Лимит модели исчерпан или достигнут бюджет расходов. Проверьте квоты выбранного провайдера; повтор не выполняется автоматически.',
          'timeout':'Модель не ответила вовремя. Результаты предыдущих этапов сохранены.',
          'invalid_response':'Ответ модели не прошёл проверку схемы.',
          'temporary':'Сервис модели временно недоступен.',
          'execution':'Модель не поддерживает выбранный способ вызова или недоступна.',
          'output_limit':'Вход или ответ модели превышает лимит.'}

class Store:
    def __init__(self,path):
        self.path=str(path);Path(path).parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS plans(run TEXT PRIMARY KEY, project TEXT, revision INTEGER, snapshot TEXT); CREATE TABLE IF NOT EXISTS actions(project TEXT, action TEXT, digest TEXT, run TEXT, PRIMARY KEY(project,action));')
    def get(self,pid,run=None):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT snapshot FROM plans WHERE project=? AND run=?',(pid,run)).fetchone() if run else db.execute('SELECT snapshot FROM plans WHERE project=? ORDER BY revision DESC LIMIT 1',(pid,)).fetchone()
        if not row: return None
        return json.loads(row[0])
    def save(self,state):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE plans SET snapshot=? WHERE run=?',(json.dumps(state,ensure_ascii=False),state['run_id']))
    def history(self,pid):
        with sqlite3.connect(self.path) as db:
            return [json.loads(r[0]) for r in db.execute('SELECT snapshot FROM plans WHERE project=? ORDER BY revision DESC',(pid,))]
    def create(self,state,action,digest):
        with sqlite3.connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO plans VALUES(?,?,?,?)',(state['run_id'],state['project_id'],state['revision'],json.dumps(state,ensure_ascii=False)))
            db.execute('INSERT INTO actions VALUES(?,?,?,?)',(state['project_id'],action,digest,state['run_id']))
    def prior(self,pid,action,digest):
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT digest,run FROM actions WHERE project=? AND action=?',(pid,action)).fetchone()
        if row:
            if row[0]!=digest: raise Conflict('Идентификатор действия уже использован')
            return self.get(pid,row[1])
    def recover(self):
        with sqlite3.connect(self.path) as db:
            rows=list(db.execute('SELECT snapshot FROM plans'))
        for row in rows:
            state=json.loads(row[0])
            if state['status'] in ACTIVE:
                state.update(status='interrupted',error='Сервер перезапущен; повторите расчёт вручную.')
                self.save(state)

class Pipeline:
    def __init__(self,repo,model,path):
        self.repo,self.model,self.store=repo,model,Store(path)
        self.tasks={};self.gate=asyncio.Lock()
        self.store.recover()
    def current(self,pid):
        state=self.store.get(pid)
        if state:
            brief=self.repo.get_project(pid)
            state['stale']=brief['status']!='confirmed' or brief.get('accepted_artifact_id')!=state['source_brief_id']
        return state
    def fresh(self,state):
        brief=self.repo.get_project(state['project_id'])
        if brief['status']!='confirmed' or brief.get('accepted_artifact_id')!=state['source_brief_id']:
            raise Conflict('Требования изменились. Сначала подтвердите новый бриф и получите новые концепции.')
    async def start(self,pid,action,expected_version,concept_id=None,expected_run=None):
        if self.gate.locked(): raise Conflict('Команда уже обрабатывается')
        async with self.gate:
            body={'expected_version':expected_version,'concept_id':concept_id,'expected_run':expected_run}
            digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
            prior=self.store.prior(pid,action,digest)
            if prior:return prior
            brief=self.repo.get_project(pid)
            if brief['status']!='confirmed' or brief['state_version']!=expected_version:
                raise Conflict('Подтвердите актуальный бриф перед расчётом')
            if unsupported_exclusions(brief['artifact']['payload']):
                raise Conflict(SCOPE_MESSAGE)
            # One complete pipeline at a time. No unbounded queue of expensive runs.
            if self.tasks: raise Conflict('Другой расчёт ещё выполняется. Дождитесь завершения.')
            previous=self.store.get(pid)
            revision=max(brief['revision'], previous['revision']+1 if previous else 1)
            identity={'schema_version':'0.1.0','project_id':pid,'run_id':str(uuid4()),'revision':revision}
            base=deepcopy(brief['artifact']);base.update(identity,artifact_id=str(uuid4()))
            catalog=load('catalog.json')
            state={**identity,'source_brief_id':brief['accepted_artifact_id'],'status':'designing' if concept_id is None else 'planning',
                   'bundle':{**identity,'artifacts':[base],'sources':[SOURCE]+catalog['sources'],'offers':catalog['offers']},
                   'model_log':[],'error':None,'selected_concept_id':concept_id}
            if concept_id is not None:
                if not previous or previous['run_id']!=expected_run: raise Conflict('Варианты устарели; обновите страницу')
                self.fresh(previous)
                options=next((a for a in previous['bundle']['artifacts'] if a['kind']=='design_options'),None)
                if not options or concept_id not in {c['id'] for c in options['payload']['concepts']}:
                    raise Conflict('Выберите существующую концепцию')
                options=deepcopy(options);options.update(identity,artifact_id=str(uuid4()),input_artifact_ids=[base['artifact_id']])
                state['bundle']['artifacts'].append(options)
            self.store.create(state,action,digest)
            task=asyncio.create_task(self._run(state))
            self.tasks[state['run_id']]=task
            task.add_done_callback(lambda _:self.tasks.pop(state['run_id'],None))
            return deepcopy(state)
    def context(self,state,stage,selected=None):
        b=state['bundle']
        return make_context(b,stage,b['artifacts'],b['sources'],b['offers'],selected_concept_id=selected)
    def record(self,state,status):
        self.fresh(state);state['status']=status;self.store.save(state)
    async def finish_plan(self, state):
        """Original basket plus at most two revisions, within the same time budget."""
        upstream = deepcopy(state['bundle']['artifacts'])
        choices, tried = None, []
        state['basket_attempts'] = []
        reason = 'Исходный выбор снабженца.'
        for attempt in range(3):
            self.fresh(state)
            state['bundle']['artifacts'] = deepcopy(upstream)
            # Do not display explanations or prices from a previous attempt.
            for key in ('purchases', 'estimate_explanation', 'planner', 'controller'):
                state.pop(key, None)
            self.record(state, 'procurement')
            purchases = await procurement.run(state['bundle'], model=self.model, choices=choices)
            self.fresh(state)
            state['bundle']['sources'] = purchases['sources']
            state['bundle']['artifacts'] += [purchases['procurement'], purchases['estimate']]
            state['purchases'] = purchases['view']
            state['estimate_explanation'] = purchases['explanations']
            state['model_log'] += purchases['model_log']
            self.record(state, 'audit')
            reviewed = await planning.run(state['bundle'], model=self.model)
            self.fresh(state)
            state['bundle'] = reviewed['bundle']
            state['model_log'] += reviewed['model_log']
            state['planner'], state['controller'] = reviewed['planner'], reviewed['controller']
            tried.append(purchases['choices'])
            state['basket_attempts'].append({
                'number': attempt + 1, 'reason': reason, 'choices': purchases['choices'],
                'total_minor': purchases['estimate']['payload']['total_minor'],
                'finish_at': reviewed['schedule']['payload']['finish_at'],
                'status': reviewed['bundle']['status'], 'bundle': deepcopy(reviewed['bundle'])})
            self.store.save(state)
            if limits(state['bundle']) in (None, (0, 0)):
                state['revision_note'] = ('Бюджет и срок соблюдены. Остальные замечания показаны в аудите.'
                                          if limits(state['bundle']) == (0, 0) else
                                          'Для автоматического пересмотра недостаточно подтверждённых сумм или дат.')
                break
            if attempt == 2:
                state['revision_note'] = 'Достигнут предел двух пересмотров. Ограничения требуют решения пользователя.'
                break
            choices, reason = await alternative(state['bundle'], tried)
            if choices is None:
                state['revision_note'] = reason
                break
        if state['bundle']['status'] == 'completed':
            validate_bundle(state['bundle'])
        self.record(state, state['bundle']['status'])
    async def _run(self,state):
        try:
            async with asyncio.timeout(480):
                if state['selected_concept_id'] is None:
                    _,design,meta=await generate_design(self.model,self.context(state,'design_options'))
                    self.fresh(state)
                    state['bundle']['artifacts'].append(design)
                    state['model_log'].append({'agent':'дизайнер',**meta})
                    self.record(state,'choosing');return
                ctx=self.context(state,'design_selection',state['selected_concept_id'])
                _,selection=call_stage(lambda c:output(c,{'concept_id':c['selected_concept_id']}),ctx)
                state['bundle']['artifacts'].append(selection)
                result,work=call_stage(work_plan_handler(),self.context(state,'work_plan'))
                if result['status']!='ok':raise ValueError(result['error']['message'])
                state['bundle']['artifacts'].append(work)
                await self.finish_plan(state)
        except asyncio.CancelledError:
            state.update(status='interrupted',error='Расчёт остановлен. Предыдущие результаты сохранены.')
            self.store.save(state);raise
        except Exception as error:
            code=error.code if isinstance(error,ModelError) else None
            message=ERRORS.get(code,'Расчёт не завершён. Проверьте данные и повторите вручную.')
            if isinstance(error,Conflict):message=str(error)
            if isinstance(error,ValueError) and not code:message='План не прошёл проверку входов или совместимости. Проверьте размеры и выбранную концепцию.'
            state.update(status='failed',error=message,error_code=code or type(error).__name__)
            self.store.save(state)
    async def cancel(self,pid):
        state=self.store.get(pid)
        if not state or state['run_id'] not in self.tasks:raise Conflict('Нет активного расчёта')
        task=self.tasks[state['run_id']];task.cancel();await asyncio.gather(task,return_exceptions=True)
        saved=self.store.get(pid)
        if saved['status'] in ACTIVE:
            saved.update(status='interrupted',error='Расчёт остановлен. Предыдущие результаты сохранены.')
            self.store.save(saved)
        return saved
    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
