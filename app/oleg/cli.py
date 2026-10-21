"""Контрольный прогон: python -m app.oleg.cli [--model]. Без --model модель не вызывается."""
import argparse
import asyncio
import json
from pathlib import Path

from contracts.handoff import verify_handoffs
from app.oleg import service

LOG = Path(__file__).resolve().parents[2]/'.scratch/oleg-mvp/results'


def rub(minor):
    return 'неизвестно' if minor is None else f"{minor/100:,.2f} ₽".replace(',', ' ')


async def main(args):
    bundle = service.example_bundle()
    model = None
    if args.model:
        from app.filipp.model import CodexModel
        model = CodexModel()
    data = await service.run(bundle, choices=None if args.model else service.example_choices(), model=model)
    v = data['view']
    print(f"Режим: {'модель выбрала и объяснила' if data['mode'] == 'model' else 'выбор из учебного примера, модель не вызывалась'}")
    for p in v['purchases']:
        print(f"  {p['requirement_id']}: нужно {p['need_with_waste']['value']} {p['need_with_waste']['unit']} → {p['offer_id']} × {p['pack_count']} = {rub(p['amount_minor'])}")
    for d in v['deliveries']:
        print(f"  доставка {d['supplier_id']}: {rub(d['amount_minor'])}")
    print(f"Материалы {rub(v['subtotals']['material'])}, работа {rub(v['subtotals']['labor'])}, доставка {rub(v['subtotals']['delivery'])}")
    print(f"База {rub(v['base_minor'])}, резерв {v['reserve_percent']}% = {rub(v['reserve_minor'])}, итог {rub(v['total_minor'])}; "
          f"бюджет {rub(v['budget_minor'])}: {v['budget_status']}")
    if data['mode'] == 'manual':
        assembled = service.assemble_bundle(json.loads(service.EXAMPLE.read_text(encoding='utf-8')) | {'offers': bundle['offers']},
                                            data['procurement'], data['estimate'], data['sources'])
        verify_handoffs(assembled)
        print('Свои документы подставлены в общий пример: validate_bundle и восемь передач проверены.')
    if args.model:
        LOG.mkdir(parents=True, exist_ok=True)
        path = LOG/'model-run.json'
        path.write_text(json.dumps({k: data[k] for k in ('mode', 'choices', 'reasons', 'explanations', 'model_log', 'procurement', 'estimate')},
                                   ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Журнал вызова модели: {path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', action='store_true', help='настоящий вызов Codex по подписке')
    asyncio.run(main(parser.parse_args()))
