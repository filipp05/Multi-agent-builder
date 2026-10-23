'use strict';
const $ = id => document.getElementById(id);
const owners = {filipp: 'Филипп', misha: 'Миша', oleg: 'Олег', karina: 'Карина'};
let active = false;

function readBundle() {
  try { return JSON.parse($('bundle').value); } catch { throw Error('Комплект должен содержать корректный JSON.'); }
}
function byKind(bundle, kind) {
  const doc = bundle.artifacts.find(a => a.kind === kind);
  if (!doc) throw Error(`В комплекте нет документа ${kind}.`);
  return doc.payload;
}
function loadControls(bundle) {
  const b = byKind(bundle, 'brief');
  $('start-date').value = b.start_date; $('deadline').value = b.deadline;
  $('day-hour').value = b.calendar.day_start_hour; $('day-hours').value = b.calendar.hours_per_day;
  for (const box of $('weekdays').querySelectorAll('input')) box.checked = b.calendar.work_weekdays.includes(Number(box.value));
}
function applyControls(bundle) {
  const b = byKind(bundle, 'brief');
  b.start_date = $('start-date').value; b.deadline = $('deadline').value;
  b.calendar.day_start_hour = Number($('day-hour').value); b.calendar.hours_per_day = Number($('day-hours').value);
  b.calendar.work_weekdays = [...$('weekdays').querySelectorAll('input:checked')].map(x => Number(x.value));
  if (!b.start_date || !b.deadline || !b.calendar.work_weekdays.length || !$('day-hour').value || !$('day-hours').value) throw Error('Задайте обе даты, рабочие часы и хотя бы один рабочий день.');
}
function list(id, items) {
  $(id).replaceChildren();
  for (const text of items) { const li = document.createElement('li'); li.textContent = text; $(id).append(li); }
}
const dateTime = value => value && Number.isFinite(new Date(value).getTime()) ? new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', dateStyle: 'short', timeStyle: 'short'}).format(new Date(value)) : 'Неизвестно или неверная дата';
function render(data) {
  const v = data.view;
  $('result').hidden = false;
  $('mode').textContent = data.mode === 'model' ? 'Модель и программные проверки' : 'Только программные проверки';
  $('finish').textContent = dateTime(v.finish_at);
  $('deadline-view').textContent = v.deadline ? v.deadline.split('-').reverse().join('.') : 'Не указан';
  $('outcome').textContent = v.status === 'completed' ? 'Проверено, ограничения соблюдены' : 'Есть нерешённые вопросы';
  $('deadline-status').textContent = {satisfied: 'Расчётный график укладывается в желаемый срок.', violated: 'Расчётное окончание позже желаемого срока. Требуется согласовать изменение плана.', unknown: 'Срок не подтверждён: дата поставки неизвестна. График предварительный.'}[v.deadline_status] || 'Срок требует проверки.';
  $('schedule-rows').replaceChildren();
  const titles = Object.fromEntries(v.rows.map(row => [row.task_id, row.title]));
  for (const row of v.rows) {
    const tr = document.createElement('tr');
    for (const text of [row.title, row.predecessor_ids.map(id => titles[id] || id).join(', ') || '—', dateTime(row.start_at), dateTime(row.finish_at), `${row.labor_hours} ч`, `${row.wait_after_hours} ч`]) {
      const td = document.createElement('td'); td.textContent = text; tr.append(td);
    }
    $('schedule-rows').append(tr);
  }
  $('planner-summary').textContent = data.planner?.summary || 'Даты рассчитаны кодом. Объяснение моделью не запрашивалось.';
  list('calendar-notes', data.planner?.calendar_notes || []);
  list('task-explanations', (data.planner?.task_explanations || []).map(x => x.text));
  $('audit-status').textContent = {passed: 'Проверка пройдена. Итоговый успех также зависит от бюджета и срока.', failed: 'Найдены ошибки: требуется исправление.', incomplete: 'Проверка не завершена: есть неизвестные данные или не запускался смысловой контролёр.'}[v.audit_status];
  $('controller-summary').textContent = data.controller?.summary || '';
  $('findings').replaceChildren();
  for (const f of v.findings) {
    const box = document.createElement('article'); box.className = `finding ${f.severity}`;
    const title = document.createElement('strong'); title.textContent = `${f.severity === 'blocking' ? 'Ошибка' : 'Замечание'} · ${f.code}`;
    const message = document.createElement('p'); message.textContent = f.message;
    const where = document.createElement('small'); where.textContent = `Ответственный: ${owners[f.owner] || f.owner}. Документы: ${f.artifact_ids.join(', ')}${f.item_ids.length ? `. Элементы: ${f.item_ids.join(', ')}` : ''}`;
    box.append(title, message, where); $('findings').append(box);
  }
  if (!v.findings.length) { const p = document.createElement('p'); p.textContent = 'Программные проверки не обнаружили расхождений.'; $('findings').append(p); }
  list('assumptions', v.assumptions);
  $('raw').textContent = JSON.stringify({schedule: data.schedule || data.bundle.artifacts.find(a => a.kind === 'schedule'), audit: data.audit}, null, 2);
}
async function execute(auditOnly) {
  if (active) return;
  active = true; $('status').className = ''; $('result').hidden = true;
  for (const control of document.querySelectorAll('button,input,textarea,select')) control.disabled = true;
  try {
    const bundle = readBundle(); applyControls(bundle);
    $('bundle').value = JSON.stringify(bundle, null, 2);
    $('status').textContent = $('use-model').checked ? 'Работают планировщик и контролёр. Вызов модели может занять несколько минут…' : 'Считаем и проверяем…';
    const response = await fetch(`/karina/api/${auditOnly ? 'audit' : 'run'}`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bundle, use_model: $('use-model').checked, calendar_wishes: $('wishes').value})});
    const data = await response.json();
    if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    render(data); $('status').textContent = 'Расчёт завершён. Результат проверки показан ниже.';
  } catch (error) { $('status').className = 'error'; $('status').textContent = `Не удалось завершить проверку: ${error.message}`; }
  finally { active = false; for (const control of document.querySelectorAll('button,input,textarea,select')) control.disabled = false; }
}
async function loadExample() {
  try {
    const response = await fetch('/karina/api/example');
    if (!response.ok) throw Error('Не удалось загрузить пример.');
    const data = await response.json();
    $('bundle').value = JSON.stringify(data.bundle, null, 2); loadControls(data.bundle);
    $('example-note').textContent = data.label; $('status').className = ''; $('status').textContent = 'Пример готов. Можно изменить календарь или проверить исходные документы.';
    $('result').hidden = true; $('fault').value = 'none';
  } catch (error) { $('status').className = 'error'; $('status').textContent = error.message; }
}
$('run').addEventListener('click', () => execute(false));
$('audit').addEventListener('click', () => execute(true));
$('load').addEventListener('click', loadExample);
$('bundle').addEventListener('change', () => { try { loadControls(readBundle()); $('example-note').textContent = 'Загружен изменённый комплект. Проверьте происхождение данных.'; } catch (e) { $('status').textContent = e.message; } });
$('inject').addEventListener('click', () => {
  try {
    const bundle = readBundle(); const fault = $('fault').value;
    if (fault === 'missing-line') byKind(bundle, 'estimate').lines.pop();
    if (fault === 'duplicate-line') { const lines = byKind(bundle, 'estimate').lines; lines.push({...lines[0], id: crypto.randomUUID()}); }
    if (fault === 'late-delivery') byKind(bundle, 'procurement').deliveries[0].available_on = '2026-11-10';
    if (fault === 'broken-wait') byKind(bundle, 'work_plan').tasks.find(t => t.id === 'paint-task').wait_after_hours = '48';
    $('bundle').value = JSON.stringify(bundle, null, 2); $('result').hidden = true;
    $('status').textContent = fault === 'none' ? 'Выберите ошибку для внесения.' : 'Документы изменены. Нажмите «Проверить существующий план».';
  } catch (e) { $('status').textContent = e.message; }
});
loadExample();
