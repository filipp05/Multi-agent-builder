"""Karina browser acceptance using an explicit model double."""

from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect
import pytest

from app.planning.api import create_app
from karina_helpers import FakeModel



@pytest.fixture
def page():
    with TestClient(create_app(FakeModel())) as client, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 960})

        def route(r):
            from urllib.parse import urlsplit
            req = r.request
            response = client.request(req.method, urlsplit(req.url).path, content=req.post_data,
                                      headers={'content-type': 'application/json'})
            r.fulfill(status=response.status_code, body=response.content,
                      headers={'content-type': response.headers.get('content-type', 'text/plain')})
        page.route('**/*', route)
        page.goto('http://localhost/karina/')
        expect(page.get_by_role('status')).to_contain_text('Пример готов')
        yield page
        browser.close()


def test_browser_builds_schedule_and_distinguishes_program_checks_from_full_audit(page):
    page.get_by_role('button', name='Построить график и проверить').click()
    expect(page.locator('#finish')).to_have_text('28.10.2026, 17:00')
    expect(page.locator('#outcome')).to_have_text('Есть нерешённые вопросы')
    expect(page.locator('#audit-status')).to_contain_text('не запускался смысловой контролёр')
    page.get_by_label('Подключить настоящую модель для объяснения и смысловой проверки').check()
    page.get_by_role('button', name='Построить график и проверить').click()
    expect(page.locator('#outcome')).to_have_text('Проверено, ограничения соблюдены')
    expect(page.locator('#schedule-rows tr')).to_have_count(3)
    page.get_by_label('Желаемое окончание', exact=True).fill('2026-10-26')
    page.get_by_role('button', name='Построить график и проверить').click()
    expect(page.locator('#deadline-status')).to_contain_text('позже желаемого срока')
    expect(page.locator('#outcome')).to_have_text('Есть нерешённые вопросы')


@pytest.mark.parametrize('fault, code', [('missing-line', 'MISSING_LINE'), ('duplicate-line', 'DUPLICATE_LINE'),
                                       ('late-delivery', 'BEFORE_DELIVERY'), ('broken-wait', 'TECHNOLOGICAL_WAIT')])
def test_browser_exposes_intentional_errors_without_repairing_the_schedule(page, fault, code):
    page.get_by_text('Документы плана и проверка ошибок', exact=True).click()
    page.get_by_label('Пример для проверки').select_option(fault)
    page.get_by_role('button', name='Внести ошибку в документы').click()
    page.get_by_role('button', name='Проверить существующий план').click()
    expect(page.locator('#audit-status')).to_contain_text('Найдены ошибки')
    expect(page.locator('#findings')).to_contain_text(code)
    expect(page.locator('#outcome')).to_have_text('Есть нерешённые вопросы')


def test_browser_shows_untrusted_titles_as_text_and_saves_preview(page, tmp_path):
    import json
    page.get_by_text('Документы плана и проверка ошибок', exact=True).click()
    data = json.loads(page.locator('#bundle').input_value())
    next(a for a in data['artifacts'] if a['kind'] == 'work_plan')['payload']['tasks'][0]['title'] = 'Подготовка <script>window.bad=1</script>'
    page.locator('#bundle').fill(json.dumps(data, ensure_ascii=False))
    page.get_by_role('button', name='Построить график и проверить').click()
    expect(page.locator('#schedule-rows')).to_contain_text('<script>window.bad=1</script>')
    assert page.evaluate('window.bad') is None
    page.get_by_role('button', name='Загрузить пример').click()
    page.get_by_text('Документы плана и проверка ошибок', exact=True).click()
    page.get_by_role('button', name='Построить график и проверить').click()
    expect(page.locator('#result')).to_be_visible()
    output = tmp_path/'preview'
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output/'browser-preview.png'), full_page=True)
