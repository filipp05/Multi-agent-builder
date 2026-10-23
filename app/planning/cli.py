"""Run the local example: python -m app.planning.cli [--model] [--audit-only]."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from app.brief.model import CodexModel, ModelError
from app.planning import service
from app.planning.common import ROOT


async def main(args):
    bundle = json.loads(args.input.read_text()) if args.input else service.example_bundle()
    model = CodexModel(model=os.environ.get('KARINA_MODEL', 'gpt-6-astra')) if args.model else None
    output = args.output or ROOT/'.scratch/karina-mvp/results'/('live-run.json' if args.model else 'checks-run.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'started_at': datetime.now(timezone.utc).isoformat(), 'requested_model': getattr(model, 'model', None),
               'input_source': str(args.input) if args.input else 'contracts/examples/completed.json'}
    try:
        data = await (service.audit_existing(bundle, model=model) if args.audit_only else
                      service.run(bundle, model=model, wishes=args.wishes))
    except (ModelError, ValueError, KeyError, TypeError) as error:
        receipt.update(status='failed', error=getattr(error, 'code', type(error).__name__),
                       metadata=getattr(error, 'metadata', {}), message=str(error))
        output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(f'Запуск не завершён: {receipt["error"]}. Журнал: {output}')
        return 2
    receipt.update(status='finished', result=data)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
    view = data['view']
    print('Режим:', 'настоящая модель' if data['mode'] == 'model' else 'только программные проверки, смысловая проверка не запускалась')
    for row in view['rows']:
        print(f'{row["task_id"]}: {row["start_at"]} → {row["finish_at"]}; пауза после {row["wait_after_hours"]} ч')
    print(f'Окончание: {view["finish_at"]}; срок: {view["deadline_status"]}; аудит: {view["audit_status"]}; комплект: {view["status"]}')
    for finding in view['findings']:
        print(f'{finding["severity"]} {finding["code"]}: {finding["message"]}')
    print('Журнал и документы:', output)
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Полный JSON-комплект, например с намеренной ошибкой')
    parser.add_argument('--output', type=Path, help='Путь журнала с результатом; по умолчанию локальная .scratch/karina-mvp/')
    parser.add_argument('--model', action='store_true', help='Настоящие вызовы планировщика и контролёра через общий CodexModel')
    parser.add_argument('--audit-only', action='store_true', help='Проверить присланный график, не перестраивая его')
    parser.add_argument('--wishes', default='', help='Пожелания к календарю для объяснения моделью; не меняют подтверждённый календарь')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
