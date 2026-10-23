"""Navigation regressions. No live model or credentials."""
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from app.main import create_app
from test_mvp import Model

@pytest.fixture
def ui(tmp_path):
    with TestClient(create_app(data_dir=tmp_path,model=Model())) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1280,'height':960},reduced_motion='reduce')
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def route(r):
            req=r.request
            res=client.request(req.method,urlsplit(req.url).path,content=req.post_data,headers={'content-type':'application/json'})
            r.fulfill(status=res.status_code,body=res.content,headers=dict(res.headers))
        page.route('**/*',route)
        yield client,page
        assert not errors,errors
        browser.close()

def confirmed(client,text='Моя спальня'):
    fields={'room.length_m':'5','room.width_m':'4','room.height_m':'2.7','room.openings_m2':'3','budget_minor':20000000,'region':'Москва','start_date':'2026-11-01','deadline':'2026-12-01','room.surface_condition':'prepared'}
    s=client.post('/api/projects',json={'action_id':str(uuid4()),'input':{'text':text,'fields':fields}}).json()
    for _ in range(200):
        s=client.get('/api/projects/'+s['project_id']).json()
        if s['status']=='ready_to_confirm':break
        time.sleep(.01)
    assert s['status']=='ready_to_confirm'
    return client.post('/api/projects/'+s['project_id']+'/confirm',json={'action_id':str(uuid4()),'expected_version':s['state_version']}).json()

def test_project_card_opens_visible_requirements_and_exact_plan(ui):
    client,page=ui;a=confirmed(client,'Спальня');b=confirmed(client,'Гостиная')
    page.goto('http://localhost/')
    page.get_by_role('button',name='Спальня').click()
    expect(page.locator('#editor')).to_be_hidden()
    expect(page.locator('#project')).to_be_in_viewport()
    expect(page.locator('#project')).to_be_focused()
    expect(page.locator('#open-plan')).to_have_attribute('href','/plan?project='+a['project_id'])
    expect(page.locator('#projects [aria-current=true]')).to_contain_text('Спальня')
    page.locator('#open-plan').click()
    expect(page.locator('#projects')).to_have_value(a['project_id'])
    page.locator('#projects').select_option(b['project_id'])
    expect(page.locator('#brief')).to_have_text('Гостиная')
    page.locator('#back').click()
    expect(page.locator('#project-description')).to_have_text('Гостиная')
    page.reload()
    expect(page.locator('#editor')).to_be_hidden()
    expect(page.locator('#project-description')).to_have_text('Гостиная')

def test_edit_cancel_save_and_new_are_distinct(ui):
    client,page=ui;s=confirmed(client)
    page.goto('http://localhost/?project='+s['project_id'])
    page.locator('#edit').click()
    expect(page.locator('#submit-brief')).to_be_hidden()
    page.locator('[name=text]').fill('Отменённый черновик')
    page.locator('#cancel-edit').click()
    expect(page.locator('#project-description')).to_have_text('Моя спальня')
    page.locator('#edit').click()
    page.locator('[name=text]').fill('Обновлённая спальня')
    page.locator('#save-edit').click()
    expect(page.locator('#confirm')).to_be_visible()
    assert len(client.get('/api/projects').json())==1
    expect(page.locator('#next-step')).to_be_hidden()
    page.locator('#new-project').click()
    expect(page.locator('#editor')).to_be_visible()
    expect(page.locator('#submit-brief')).to_be_visible()
    expect(page.locator('#project')).to_be_hidden()
    expect(page.locator('[name=text]')).to_have_value('')

def test_empty_and_invalid_project_are_actionable(ui):
    client,page=ui
    page.goto('http://localhost/plan')
    expect(page.locator('#status')).to_have_text('Начните с требований')
    expect(page.locator('#design')).to_be_disabled()
    page.locator('#back').click()
    expect(page.locator('#editor')).to_be_visible()
    page.goto('http://localhost/plan?project='+str(uuid4()))
    expect(page.locator('#error')).to_contain_text('не найден')
    expect(page.locator('#design')).to_be_disabled()
    page.goto('http://localhost/?project='+str(uuid4()))
    expect(page.locator('#error')).to_contain_text('больше не доступен')
    expect(page.locator('#editor')).to_be_visible()

def test_download_stale_and_mobile(ui):
    client,page=ui;s=confirmed(client)
    page.goto('http://localhost/plan?project='+s['project_id'])
    page.locator('#design').click()
    expect(page.locator('#concepts article')).to_have_count(2)
    expect(page.locator('.color-chips svg')).to_have_count(4)
    page.get_by_role('button',name='Выбрать и рассчитать').first.click()
    expect(page.locator('#status')).to_have_text('План прошёл проверки',timeout=15000)
    expect(page.locator('#result')).to_contain_text('Резерв 10%')
    expect(page.locator('#result')).to_contain_text('Краска ·')
    page.get_by_text('Скачать данные проекта',exact=True).click()
    with page.expect_download() as download:page.locator('#download').click()
    data=json.loads(Path(download.value.path()).read_text())
    assert len(data['artifacts'])==8
    for width in [1280,768,390,320]:
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),width
    p=client.get('/api/projects/'+s['project_id']).json()
    client.post('/api/projects/'+s['project_id']+'/edit',json={'action_id':str(uuid4()),'expected_version':p['state_version'],'input':{'text':'Другой ремонт','fields':{}}})
    page.reload()
    expect(page.locator('#status')).to_have_text('Требования изменились')
    expect(page.get_by_role('button',name='Пересчитать этот вариант')).to_be_disabled()

def test_model_failure_retry_and_cancel(ui):
    import asyncio
    from app.brief.model import ModelError
    client,page=ui;s=confirmed(client)
    class Controlled(Model):
        mode='fail'
        async def generate(self,r):
            if 'concepts' in r.schema['properties']:
                if self.mode=='fail':raise ModelError('quota',{})
                if self.mode=='slow':await asyncio.sleep(30)
            return await super().generate(r)
    model=Controlled();client.app.state.pipeline.model=model
    page.goto('http://localhost/plan?project='+s['project_id'])
    page.locator('#design').click()
    expect(page.locator('#status')).to_have_text('Расчёт не завершён')
    expect(page.locator('#error')).to_contain_text('Лимит модели исчерпан')
    expect(page.locator('#design')).to_be_enabled()
    model.mode='slow';page.locator('#design').click()
    expect(page.locator('#cancel')).to_be_visible()
    expect(page.locator('#design')).to_be_disabled()
    page.locator('#cancel').click()
    expect(page.locator('#status')).to_have_text('Расчёт прерван')
    model.mode='ok';page.locator('#design').click()
    expect(page.locator('#concepts article')).to_have_count(2)
    expect(page.locator('#error')).to_have_text('')


def test_main_layout_on_small_screens(ui):
    client,page=ui;confirmed(client)
    page.goto('http://localhost/')
    target=Path('.scratch/misha-mvp/results');target.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(target/'redesign-desktop.png'),full_page=True)
    for width in [768,390,320]:
        page.set_viewport_size({'width':width,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),width
        page.get_by_role('button',name='Моя спальня').click()
        expect(page.locator('#open-plan')).to_be_in_viewport()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),width
        page.locator('#edit').click()
        expect(page.locator('[name=text]')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),width
        page.locator('#cancel-edit').click()
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path=str(target/'redesign-mobile.png'),full_page=True)
