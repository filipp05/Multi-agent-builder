"""Complete A→B→C→D UI acceptance. Model double; never live credentials."""
import os
from pathlib import Path
from urllib.parse import urlsplit
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
from app.main import create_app
from test_mvp import Model


def test_complete_flow_and_reload(tmp_path):
    model=Model()
    with TestClient(create_app(data_dir=tmp_path,model=model)) as client, sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1280,'height':960})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def route(r):
            req=r.request
            response=client.request(req.method,urlsplit(req.url).path,content=req.post_data,
                                    headers={'content-type':'application/json'})
            r.fulfill(status=response.status_code,body=response.content,
                      headers={'content-type':response.headers.get('content-type','text/plain')})
        page.route('**/*',route)
        page.goto('http://localhost/')
        page.locator('[name=text]').fill('Покраска и ламинат <script>window.bad=1</script>')
        fields={'room.length_m':'5','room.width_m':'4','room.height_m':'2.7','room.openings_m2':'3',
                'budget_rub':'200000','region':'Москва','start_date':'2026-11-01','deadline':'2026-12-01'}
        for k,v in fields.items():page.locator(f'[name="{k}"]').fill(v)
        page.get_by_text('Календарь и дополнительные ограничения',exact=True).click()
        page.locator('[name="room.surface_condition"]').select_option('prepared')
        page.get_by_role('button',name='Разобрать пожелания').click()
        expect(page.locator('#confirm')).to_be_visible()
        page.get_by_role('button',name='Подтвердить требования').click()
        expect(page.locator('#status')).to_have_text('Требования подтверждены')
        page.goto('http://localhost/plan')
        page.get_by_role('button',name='Получить две концепции').click()
        expect(page.locator('#concepts article')).to_have_count(2)
        page.get_by_role('button',name='Выбрать и рассчитать').first.click()
        expect(page.locator('#status')).to_have_text('План прошёл проверки',timeout=15000)
        expect(page.locator('#stages li')).to_have_count(8)
        expect(page.locator('#result')).to_contain_text('Независимый аудит: пройден')
        assert page.evaluate('window.bad') is None
        assert not errors,errors
        before=page.locator('#documents').text_content()
        page.reload()
        expect(page.locator('#status')).to_have_text('План прошёл проверки')
        expect(page.locator('#documents')).to_have_text(before)
        target=Path('.scratch/misha-mvp/results');target.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target/'browser-preview.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.screenshot(path=str(target/'browser-mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        browser.close()
