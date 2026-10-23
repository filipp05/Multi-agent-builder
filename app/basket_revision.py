"""Bounded search over the existing catalogue; never changes the user's brief."""
import asyncio
from datetime import datetime, time
from itertools import product
from zoneinfo import ZoneInfo

from app.procurement import agents, service
from app.procurement.stages import load
from app.planning.schedule import build_schedule
from contracts.handoff import make_context

MAX_BASKETS = 128


def documents(bundle):
    return {a['kind']: a['payload'] for a in bundle['artifacts']}


def limits(bundle):
    docs = documents(bundle)
    estimate, schedule, brief = (docs[k] for k in ('estimate', 'schedule', 'brief'))
    if estimate['total_minor'] is None or schedule['deadline_status'] == 'unknown':
        return None
    deadline = datetime.combine(datetime.fromisoformat(brief['deadline']).date(), time.max,
                                ZoneInfo(brief['calendar']['timezone']))
    finish = datetime.fromisoformat(schedule['finish_at'])
    return (max(0, estimate['total_minor'] - brief['budget_minor']),
            max(0, (finish - deadline).total_seconds()))


async def alternative(bundle, tried):
    """Find a strictly better basket without worsening either constraint."""
    before = limits(bundle)
    if before is None or before == (0, 0):
        return None, 'Бюджет и срок не требуют пересмотра корзины либо данных недостаточно.'
    rates = load('rates.json')
    _, upstream, _, context = service.contexts_from(bundle, rates)
    candidates = agents.candidates(context, rates)
    keys = sorted(candidates)
    options = [[x['offer_id'] for x in candidates[k]
                if x['available_on'] is not None and x['delivery_minor'] is not None] for k in keys]
    total = 1
    for values in options:
        total *= len(values)
    if total > MAX_BASKETS:
        return None, 'Каталог превышает предел автоматического поиска; нужен ручной выбор.'
    best = None
    base = {**bundle, 'artifacts': [a for a in bundle['artifacts'] if a['kind'] in service.UPSTREAM]}
    for values in product(*options):
        await asyncio.sleep(0)  # Cancellation remains available during local search.
        choices = dict(zip(keys, values))
        if choices in tried:
            continue
        calculated = await service.run(base, choices=choices)
        docs = upstream + [calculated['procurement'], calculated['estimate']]
        schedule, _, _ = build_schedule(make_context(
            bundle, 'schedule', docs, calculated['sources'], bundle['offers']))
        preview = {'artifacts': docs + [{'kind': 'schedule', 'payload': schedule}]}
        after = limits(preview)
        if after is None or any(a > b for a, b in zip(after, before)) or after == before:
            continue
        score = (sum(x > 0 for x in after), *after, calculated['estimate']['payload']['total_minor'],
                 schedule['finish_at'], tuple(values))
        if best is None or score < best[0]:
            best = (score, choices)
    if best is None:
        return None, 'В проверенном каталоге нет другой корзины, улучшающей бюджет или срок без ухудшения второго ограничения.'
    return best[1], 'Подобрана другая совместимая корзина с учётом упаковок, доставки, резерва и графика; требуется повторный аудит.'
