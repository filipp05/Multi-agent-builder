"""Local page and mountable router for Karina's two stages."""
import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.filipp.model import CodexModel, ModelError
from app.karina import service

BASE = Path(__file__).parent
router = APIRouter(prefix='/karina')


class RunCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bundle: dict
    use_model: bool = False
    calendar_wishes: str = Field(default='', max_length=4000)


@router.get('/')
async def page():
    return FileResponse(BASE/'static/index.html')


@router.get('/api/example')
async def example():
    return {'label': service.EXAMPLE_LABEL, 'bundle': service.example_bundle()}


async def execute(request, body, *, audit_only=False):
    try:
        if body.use_model:
            # Bound live calls per local process, like the foreman's local application.
            async with request.app.state.karina_model_lock:
                if audit_only:
                    return await service.audit_existing(body.bundle, model=request.app.state.karina_model)
                return await service.run(body.bundle, model=request.app.state.karina_model, wishes=body.calendar_wishes)
        if audit_only:
            return await service.audit_existing(body.bundle)
        return await service.run(body.bundle)
    except service.StageFailed as error:
        return JSONResponse({'detail': str(error), 'result': error.result}, status_code=422)
    except ModelError as error:
        return JSONResponse({'detail': f'Модель не завершила проверку: {error.code}. Успех не подтверждён.',
                             'code': error.code, 'metadata': error.metadata}, status_code=503)
    except (ValueError, KeyError, TypeError) as error:
        return JSONResponse({'detail': str(error), 'code': getattr(error, 'code', 'INVALID_INPUT')}, status_code=422)


@router.post('/api/run')
async def run(request: Request, body: RunCommand):
    return await execute(request, body)


@router.post('/api/audit')
async def audit(request: Request, body: RunCommand):
    return await execute(request, body, audit_only=True)


def configure(app, model=None):
    """Mount router and assets in the foreman's app without adding another model adapter."""
    app.state.karina_model = model or CodexModel(model=os.environ.get('KARINA_MODEL', 'gpt-6-astra'))
    app.state.karina_model_lock = asyncio.Semaphore(1)
    app.mount('/karina/static', StaticFiles(directory=BASE/'static'), name='karina-static')
    app.include_router(router)


def create_app(model=None):
    app = FastAPI(title='Карина: график и контроль')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])

    @app.middleware('http')
    async def local_browser(request, call_next):
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD') and origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    configure(app, model)
    return app


app = create_app()
