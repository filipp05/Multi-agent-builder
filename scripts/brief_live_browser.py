"""Explicit live browser acceptance probe against the running local app."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1200,'height':1000})
    page.route('**/*',lambda r:r.continue_() if r.request.url.startswith('http://127.0.0.1:8000/') else r.abort())
    page.goto('http://127.0.0.1:8000/')
    page.locator('[name=text]').fill('Покрасить стены и уложить ламинат.')
    for key,value in {'room.length_m':'5','room.width_m':'4','room.openings_m2':'3','budget_rub':'200000','region':'Москва','start_date':'2026-11-01','deadline':'2026-12-01'}.items():
        page.locator(f'[name="{key}"]').fill(value)
    page.get_by_role('button',name='Разобрать пожелания').click()
    expect(page.locator('#status')).to_have_text('Нужны уточнения',timeout=90000)
    page.locator('#answers input').fill('Высота 2,7 метра')
    page.get_by_role('button',name='Ответить на вопросы').click()
    expect(page.locator('#confirm')).to_be_visible(timeout=90000)
    page.get_by_role('button',name='Подтвердить требования').click()
    expect(page.locator('#status')).to_have_text('Требования подтверждены',timeout=10000)
    page.reload()
    expect(page.locator('#status')).to_have_text('Требования подтверждены')
    project_id=page.evaluate("localStorage.getItem('filipp-project')")
    snapshot=page.request.get('http://127.0.0.1:8000/api/projects/'+project_id).json()
    assert snapshot['artifact']['payload']['room']['height_m']=='2.7'
    assert snapshot['artifact']['payload']['budget_minor']==20000000
    Path('.scratch/filipp-mvp/results').mkdir(parents=True,exist_ok=True)
    page.screenshot(path='.scratch/filipp-mvp/results/live-browser.png',full_page=True)
    Path('.scratch/filipp-mvp/results/live-browser.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2))
    print(json.dumps({'status':snapshot['status'],'project_id':project_id,'height_m':'2.7','budget_minor':20000000,'provider':snapshot['model_metadata']['provider']},ensure_ascii=False))
    browser.close()
