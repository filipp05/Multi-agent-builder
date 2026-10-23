'use strict';
const $ = (id) => document.getElementById(id);
const KIND = {material: 'Материалы', labor: 'Работа', delivery: 'Доставка'};
const STATUS = {satisfied: 'укладывается в бюджет', violated: 'превышает бюджет', unknown: 'неизвестно: есть неизвестные суммы'};

function money(minor, currency) {
  if (minor === null || minor === undefined) return 'неизвестно';
  const rub = (minor / 100).toLocaleString('ru-RU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return currency === 'RUB' ? `${rub} ₽` : `${rub} ${currency}`;
}
const qty = (q) => `${q.value} ${q.unit}`;
const cite = (sources) => sources.map((s) => `${s.id} (${s.kind === 'synthetic' ? 'учебные данные' : 'наблюдение'}, ${s.captured_at.slice(0, 10)})`).join('; ');

function row(tbody, cells, cls) {
  const tr = document.createElement('tr');
  if (cls) tr.className = cls;
  for (const value of cells) {
    const td = document.createElement('td');
    td.textContent = value;
    tr.appendChild(td);
  }
  tbody.appendChild(tr);
}

function term(dl, name, value, cls) {
  const dt = document.createElement('dt'); dt.textContent = name;
  const dd = document.createElement('dd'); dd.textContent = value;
  if (cls) dd.className = cls;
  dl.append(dt, dd);
}

function render(data) {
  const v = data.view, cur = v.currency;
  $('result').hidden = false;
  $('mode').textContent = data.mode === 'model' ? 'выбор и пояснения: модель' : 'выбор задан вручную, модель не вызывалась';
  const totals = $('totals'); totals.replaceChildren();
  term(totals, 'Материалы', money(v.subtotals.material, cur));
  term(totals, 'Работа', money(v.subtotals.labor, cur));
  term(totals, 'Доставка', money(v.subtotals.delivery, cur));
  term(totals, 'База (сумма до резерва)', money(v.base_minor, cur));
  term(totals, `Резерв ${v.reserve_percent}% (правило ${v.reserve_rule})`, money(v.reserve_minor, cur));
  term(totals, 'Общий итог', money(v.total_minor, cur), 'total');
  term(totals, 'Бюджет', money(v.budget_minor, cur));
  term(totals, 'Статус', STATUS[v.budget_status], v.budget_status);
  term(totals, 'Нехватка бюджета', money(v.shortfall_minor, cur), v.shortfall_minor ? 'violated' : '');
  $('partial').hidden = v.partial_known_minor === null;
  $('partial').textContent = `Известная часть: ${money(v.partial_known_minor, cur)}. Это не полная смета: неизвестные суммы не считаются нулём.`;
  const explained = data.explanations || {lines: {}};
  $('summary').hidden = !data.explanations;
  $('summary').textContent = data.explanations ? data.explanations.summary : '';

  const purchases = $('purchases').tBodies[0]; purchases.replaceChildren();
  for (const p of v.purchases) {
    row(purchases, [`${p.requirement_id} (${p.category})`, `${qty(p.base_quantity)} + ${p.waste_percent}% = ${qty(p.need_with_waste)}`,
      `${p.offer_id} / ${p.supplier_id}`, qty(p.pack_quantity), String(p.pack_count), qty(p.purchased_quantity),
      money(p.unit_price_minor, cur), money(p.amount_minor, cur), cite(p.sources), data.reasons[p.requirement_id] || '—']);
  }
  const deliveries = $('deliveries').tBodies[0]; deliveries.replaceChildren();
  for (const d of v.deliveries) {
    row(deliveries, [d.supplier_id, d.amount_minor === 0 ? 'бесплатно (подтверждено)' : money(d.amount_minor, cur),
      d.available_on || 'неизвестна', cite(d.sources)], d.amount_minor === null ? 'unknown' : '');
  }
  const lines = $('lines').tBodies[0]; lines.replaceChildren();
  for (const l of v.lines) {
    row(lines, [l.label, KIND[l.kind], l.reference_id, money(l.amount_minor, cur), cite(l.sources), explained.lines[l.id] || '—'],
      l.amount_minor === null ? 'unknown' : '');
  }
  const list = $('assumptions'); list.replaceChildren();
  for (const a of v.assumptions) { const li = document.createElement('li'); li.textContent = a; list.appendChild(li); }
  $('room').textContent = `Сверка объёма: площадь стен по размерам комнаты ${v.room_check.wall_area_m2} м², объём работы покраски ${v.room_check.paint_task_m2.join(', ')} м².`;
  $('raw').textContent = JSON.stringify({procurement: data.procurement, estimate: data.estimate}, null, 2);
  $('log-box').hidden = !data.model_log.length;
  $('log').textContent = JSON.stringify(data.model_log, null, 2);
}

function parse(id, name) {
  const text = $(id).value.trim();
  try { return JSON.parse(text); } catch { throw new Error(`Поле «${name}» не является JSON`); }
}

async function send(withChoices) {
  const status = $('status');
  status.textContent = withChoices ? 'Считаю…' : 'Вызываю модель: выбор и пояснения могут занять до нескольких минут…';
  status.className = '';
  try {
    const body = {bundle: parse('bundle', 'Комплект'), choices: withChoices ? parse('choices', 'Выбор') : null};
    const response = await fetch('/oleg/api/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    render(data);
    status.textContent = 'Готово.';
  } catch (error) {
    status.textContent = `Отказ: ${error.message}`;
    status.className = 'error';
  }
}

async function loadExample() {
  const data = await (await fetch('/oleg/api/example')).json();
  $('bundle').value = JSON.stringify(data.bundle, null, 2);
  $('choices').value = JSON.stringify(data.choices, null, 2);
  $('label').textContent = data.label;
  $('label').hidden = false;
}

document.addEventListener('DOMContentLoaded', () => {
  $('load').addEventListener('click', loadExample);
  $('manual').addEventListener('click', () => send(true));
  $('model').addEventListener('click', () => send(false));
  loadExample();
});
