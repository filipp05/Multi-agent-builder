"""Shared closed staging app. One worker; secrets only in process environment."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from app.filipp.api import create_app as brief_app
from app.filipp.model import CodexModel
from app.providers.bedrock import BedrockModel
from app.pipeline import Pipeline
from app.oleg import api as oleg
from app.karina.api import configure as configure_karina

BASE=Path(__file__).parent
class PlanCommand(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action_id:str=Field(min_length=1,max_length=100)
    expected_version:int=Field(ge=0)
    concept_id:str|None=Field(default=None,max_length=100)
    expected_run:str|None=Field(default=None,max_length=100)

def create_app(*, data_dir=None,model=None,allowed_hosts=None):
    data=Path(data_dir or os.environ.get('RENOVATION_DATA_DIR','.scratch/deployment'))
    if model is None:
        provider=os.environ.get('MODEL_PROVIDER','bedrock')
        if provider=='codex':model=CodexModel(model=os.environ.get('CODEX_MODEL_ID','gpt-6-astra'))
        elif provider=='bedrock':
            model=BedrockModel(region=os.environ.get('AWS_REGION','us-east-2'),
                model=os.environ.get('BEDROCK_MODEL_ID','amazon.nova-lite-v1:0'),ledger=data/'usage.sqlite3',
                budget_usd=os.environ.get('MODEL_BUDGET_USD','5'),
                input_rate=os.environ.get('MODEL_INPUT_USD_PER_MILLION'),
                output_rate=os.environ.get('MODEL_OUTPUT_USD_PER_MILLION'))
        else:raise ValueError('Unknown model provider')
    hosts=allowed_hosts or os.environ.get('ALLOWED_HOSTS','localhost,127.0.0.1,testserver').split(',')
    app=brief_app(db_path=data/'brief.sqlite3',model=model,allowed_hosts=hosts)
    original=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            app.state.pipeline=Pipeline(app.state.repo,model,data/'plans.sqlite3')
            try:yield
            finally:await app.state.pipeline.close()
    app.router.lifespan_context=lifespan
    app.state.oleg_model=model
    app.mount('/oleg/static',StaticFiles(directory=oleg.BASE/'static'),name='oleg-static')
    app.include_router(oleg.router)
    configure_karina(app,model)
    app.mount('/mvp/static',StaticFiles(directory=BASE/'static'),name='mvp-static')
    @app.get('/plan')
    async def page():return FileResponse(BASE/'static/plan.html')
    @app.get('/health')
    async def health():return {'status':'ok','provider':os.environ.get('MODEL_PROVIDER','bedrock'),'model':getattr(model,'model',None),'check':'process_only'}
    @app.get('/api/projects/{pid}/plan')
    async def get_plan(pid:str):
        app.state.repo.get_project(pid)
        return app.state.pipeline.current(pid)
    @app.get('/api/projects/{pid}/plan/history')
    async def history(pid:str):
        app.state.repo.get_project(pid)
        return app.state.pipeline.store.history(pid)
    @app.post('/api/projects/{pid}/plan',status_code=202)
    async def start(pid:str,body:PlanCommand):
        return await app.state.pipeline.start(pid,body.action_id,body.expected_version,body.concept_id,body.expected_run)
    @app.post('/api/projects/{pid}/plan/cancel')
    async def cancel(pid:str):return await app.state.pipeline.cancel(pid)
    return app

app=create_app()
