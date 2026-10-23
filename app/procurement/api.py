"""Отдельная страница и API закупок и сметы. Общий запуск и API ведёт Филипп; router можно подключить к его приложению."""
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.brief.model import CodexModel, ModelError
from app.procurement import service

BASE = Path(__file__).parent


class RunCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bundle: dict
    choices: dict[str, str] | None = None


class CheckCommand(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bundle: dict
    procurement: dict
    estimate: dict


router = APIRouter(prefix='/oleg')


@router.get('/')
async def page():
    return FileResponse(BASE/'static/index.html')


@router.get('/api/example')
async def example():
    return {'label': service.EXAMPLE_LABEL, 'bundle': service.example_bundle(), 'choices': service.example_choices()}


@router.post('/api/run')
async def run(request: Request, body: RunCommand):
    try:
        return await service.run(body.bundle, choices=body.choices, model=None if body.choices is not None else request.app.state.oleg_model)
    except service.StageFailed as e:
        return JSONResponse({'detail': str(e), 'result': e.result}, status_code=422)
    except ModelError as e:
        return JSONResponse({'detail': f'Модель недоступна или ответила некорректно: {e.code}', 'code': e.code}, status_code=503)
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({'detail': str(e)}, status_code=422)


@router.post('/api/check')
async def verify(body: CheckCommand):
    try:
        findings = service.verify(body.bundle, body.procurement, body.estimate)
    except (ValueError, KeyError, TypeError) as e:
        return JSONResponse({'detail': str(e)}, status_code=422)
    return {'ok': not findings, 'findings': findings}


def create_app(model=None):
    app = FastAPI()
    app.state.oleg_model = model or CodexModel()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'testserver'])

    @app.middleware('http')
    async def csp(request, call_next):
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        return response

    app.mount('/oleg/static', StaticFiles(directory=BASE/'static'), name='oleg-static')
    app.include_router(router)
    return app


app = create_app()
