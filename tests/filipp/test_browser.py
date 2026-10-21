import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from app.filipp.api import create_app
from test_clarifications import AnswerModel


def test_browser_questions_confirmation_reload_and_html(tmp_path):
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(Path('.scratch/filipp-mvp/runtime/browsers').resolve())
    with TestClient(create_app(tmp_path/'browser.sqlite3',AnswerModel())) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        def route(r):
            req=r.request
            from urllib.parse import urlsplit
            path=urlsplit(req.url).path
            response=client.request(req.method,path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
        page.route('**/*',route)
        page.goto('http://localhost/')
        page.locator('textarea[name=text]').fill('Покраска <script>window.bad=1</script> и ламинат')
        for key,value in {'room.length_m':'5','room.width_m':'4','room.openings_m2':'3','budget_rub':'200000','region':'Москва','start_date':'2026-11-01','deadline':'2026-12-01'}.items():
            page.locator(f'[name="{key}"]').fill(value)
        page.get_by_role('button',name='Разобрать пожелания').click()
        expect(page.locator('#status')).to_have_text('Нужны уточнения')
        page.locator('#answers input').fill('2,7 м')
        page.get_by_role('button',name='Ответить на вопросы').click()
        expect(page.locator('#confirm')).to_be_visible()
        page.get_by_role('button',name='Подтвердить требования').click()
        expect(page.locator('#status')).to_have_text('Требования подтверждены')
        page.reload()
        expect(page.locator('#status')).to_have_text('Требования подтверждены')
        assert page.evaluate('window.bad') is None
        page.get_by_role('button',name='Изменить данные',exact=True).click()
        expect(page.locator('[name="calendar.hours_per_day"]')).to_be_visible()
        browser.close()


def test_delayed_poll_cannot_reopen_old_project(tmp_path):
    from test_full_brief import EmptyModel
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(Path('.scratch/filipp-mvp/runtime/browsers').resolve())
    with TestClient(create_app(tmp_path/'browser.sqlite3',EmptyModel())) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page()
        pending=[]
        def route(r):
            from urllib.parse import urlsplit
            req=r.request;path=urlsplit(req.url).path
            if req.method=='GET' and path.startswith('/api/projects/'):
                pending.append((r,path));return
            response=client.request(req.method,path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
        page.route('**/*',route)
        page.goto('http://localhost/')
        page.locator('[name=text]').fill('Покраска стен')
        page.get_by_role('button',name='Разобрать пожелания').click()
        for _ in range(50):
            if pending:break
            page.wait_for_timeout(50)
        assert pending
        page.get_by_role('button',name='Новый проект',exact=True).click()
        r,path=pending.pop()
        response=client.get(path)
        r.fulfill(status=200,body=response.content,content_type='application/json')
        page.wait_for_timeout(200)
        expect(page.locator('#project')).to_be_hidden()
        assert page.evaluate("localStorage.getItem('filipp-project')") is None
        browser.close()


def test_browser_can_cancel_running_model(tmp_path):
    from test_lifecycle import Slow
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(Path('.scratch/filipp-mvp/runtime/browsers').resolve())
    with TestClient(create_app(tmp_path/'cancel.sqlite3',Slow())) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        def route(r):
            from urllib.parse import urlsplit
            req=r.request
            response=client.request(req.method,urlsplit(req.url).path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
        page.route('**/*',route);page.goto('http://localhost/')
        page.locator('[name=text]').fill('Покрасить стены')
        page.get_by_role('button',name='Разобрать пожелания').click()
        expect(page.locator('#status')).to_have_text('Уточняем требования')
        page.get_by_role('button',name='Остановить',exact=True).click()
        expect(page.locator('#status')).to_have_text('Расчёт остановлен')
        expect(page.get_by_role('button',name='Повторить',exact=True)).to_be_visible()
        browser.close()


def test_browser_scope_warning_is_text_and_survives_reload(tmp_path):
    from app.filipp.model import ModelReply
    from test_full_brief import EmptyModel,create
    class Unsupported(EmptyModel):
        async def generate(self,request):
            return ModelReply({'candidates':[],'ambiguities':[],'unsupported_requests':['электрика','сантехника <script>window.bad=1</script>']},{})
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(Path('.scratch/filipp-mvp/runtime/browsers').resolve())
    with TestClient(create_app(tmp_path/'browser.sqlite3',Unsupported())) as client, sync_playwright() as pw:
        snapshot=create(client)
        browser=pw.chromium.launch(headless=True);page=browser.new_page()
        def route(r):
            from urllib.parse import urlsplit
            req=r.request
            response=client.request(req.method,urlsplit(req.url).path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=response.status_code,body=response.content,headers={'content-type':response.headers.get('content-type','text/plain')})
        page.route('**/*',route);page.goto('http://localhost/')
        page.evaluate('(id)=>localStorage.setItem("filipp-project",id)',snapshot['project_id']);page.reload()
        expect(page.locator('#problem')).to_contain_text('Электрику пока не можем реализовать')
        expect(page.locator('#problem')).to_contain_text('сантехника <script>')
        expect(page.locator('#completion')).to_contain_text('новый проект')
        expect(page.locator('#confirm')).to_be_hidden()
        page.reload();expect(page.locator('#problem')).to_contain_text('Электрику пока не можем реализовать')
        assert page.evaluate('window.bad') is None
        browser.close()

@pytest.mark.parametrize('action',['create','edit'])
def test_late_post_cannot_reopen_project_after_new(tmp_path,action):
    from test_full_brief import EmptyModel,create
    os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(Path('.scratch/filipp-mvp/runtime/browsers').resolve())
    with TestClient(create_app(tmp_path/'late-post.db',EmptyModel())) as client, sync_playwright() as pw:
        snapshot=create(client)
        browser=pw.chromium.launch(headless=True);page=browser.new_page();pending=[]
        def route(r):
            from urllib.parse import urlsplit
            req=r.request;path=urlsplit(req.url).path
            if req.method=='POST':pending.append(r);return
            res=client.request(req.method,path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=res.status_code,body=res.content,content_type=res.headers.get('content-type','text/plain'))
        page.route('**/*',route);page.goto('http://localhost/')
        if action=='edit':
            page.evaluate('(id)=>localStorage.setItem("filipp-project",id)',snapshot['project_id']);page.reload()
            page.get_by_role('button',name='Изменить данные',exact=True).click()
            page.get_by_role('button',name='Пересчитать изменённые требования').click()
        else:
            page.locator('[name=text]').fill('Покраска');page.get_by_role('button',name='Разобрать пожелания').click()
        for _ in range(50):
            if pending:break
            page.wait_for_timeout(20)
        assert pending
        page.get_by_role('button',name='Новый проект',exact=True).click()
        r=pending.pop()
        res=client.post(r.request.url.removeprefix('http://localhost'),content=r.request.post_data,headers={'content-type':'application/json'})
        r.fulfill(status=res.status_code,body=res.content,content_type='application/json')
        page.wait_for_timeout(200)
        expect(page.locator('#project')).to_be_hidden()
        assert page.evaluate("localStorage.getItem('filipp-project')") is None
        browser.close()
