from contextlib import asynccontextmanager
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.filipp.model import CodexModel
from app.filipp.repository import Repository,Conflict,NotFound
from app.filipp.service import Service
from app.filipp.presentation import present
from app.filipp.types import CreateCommand,CommandMeta,AnswersCommand,EditCommand

BASE=Path(__file__).parent

def create_app(db_path=None,model=None,allowed_hosts=None):
    repo=Repository(db_path or BASE.parents[1]/'.scratch/filipp-mvp/runtime/app.sqlite3')
    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(repo.recover_interrupted)
        app.state.service=Service(repo,model or CodexModel())
        yield
        await app.state.service.close()
    app=FastAPI(lifespan=lifespan)
    app.state.repo=repo
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=allowed_hosts or ['localhost','127.0.0.1','testserver'])
    app.mount('/static',StaticFiles(directory=BASE/'static'),name='static')
    templates=Jinja2Templates(directory=BASE/'templates')
    @app.middleware('http')
    async def local_origin(request,call_next):
        # Browser mutations must come from this local origin, never an external page.
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD') and origin and origin!=str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'Недопустимый источник запроса'},status_code=403)
        response=await call_next(request)
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response
    @app.exception_handler(Conflict)
    async def conflict(_,e): return JSONResponse({'detail':str(e)},status_code=409)
    @app.exception_handler(NotFound)
    async def not_found(_,e): return JSONResponse({'detail':str(e)},status_code=404)
    @app.exception_handler(ValueError)
    async def invalid(_,e): return JSONResponse({'detail':str(e)},status_code=422)
    @app.get('/')
    async def home(request:Request): return templates.TemplateResponse(request=request,name='index.html',context={})
    @app.get('/api/projects')
    async def projects(): return [present(x) for x in await asyncio.to_thread(repo.list_projects)]
    @app.get('/api/projects/{pid}')
    async def get(pid:str): return present(await asyncio.to_thread(repo.get_project,pid))
    @app.post('/api/projects',status_code=202)
    async def create(body:CreateCommand): return present(await app.state.service.create_project(body.input,body.action_id))
    @app.post('/api/projects/{pid}/answers',status_code=202)
    async def answers(pid:str,body:AnswersCommand):
        return present(await app.state.service.act(pid,'answers',body,{'answers':[x.model_dump() for x in body.answers]}))
    @app.post('/api/projects/{pid}/edit',status_code=202)
    async def edit(pid:str,body:EditCommand):
        return present(await app.state.service.act(pid,'edit',body,{'input':body.input.model_dump()}))
    @app.post('/api/projects/{pid}/confirm')
    async def confirm(pid:str,body:CommandMeta): return present(await app.state.service.act(pid,'confirm',body))
    @app.post('/api/projects/{pid}/cancel')
    async def cancel(pid:str,body:CommandMeta): return present(await app.state.service.act(pid,'cancel',body))
    @app.post('/api/projects/{pid}/retry',status_code=202)
    async def retry(pid:str,body:CommandMeta): return present(await app.state.service.act(pid,'retry',body))
    return app

app=create_app()
