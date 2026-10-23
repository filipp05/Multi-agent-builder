"""Local UI launcher. Credentials stay in CLI auth or process environment."""
import argparse
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = argparse.ArgumentParser(description='Локальный MVP ремонта: http://127.0.0.1:8000')
    result.add_argument('--provider', choices=['codex', 'bedrock'], default='codex',
                        help='codex: подписка ChatGPT; bedrock: AWS API (default: codex)')
    result.add_argument('--model', help='ID модели; по умолчанию gpt-6-astra / amazon.nova-lite-v1:0')
    result.add_argument('--port', type=int, default=8000)
    result.add_argument('--data-dir', type=Path, default=ROOT / '.scratch/local-mvp',
                        help='Каталог проектов и истории; сохраняется между запусками')
    return result


def configure(args):
    if not 1 <= args.port <= 65535:
        raise ValueError('Порт должен быть от 1 до 65535')
    if args.provider == 'codex' and not shutil.which('codex'):
        raise ValueError('Не найден Codex CLI. Установите его и выполните codex login.')
    if args.provider == 'bedrock' and not os.environ.get('AWS_BEARER_TOKEN_BEDROCK'):
        raise ValueError('Задайте AWS_BEARER_TOKEN_BEDROCK в этом терминале. Ключ в аргументы не передаётся.')
    os.environ['MODEL_PROVIDER'] = args.provider
    os.environ['RENOVATION_DATA_DIR'] = str(args.data_dir.resolve())
    os.environ['ALLOWED_HOSTS'] = '127.0.0.1,localhost'
    if args.model:
        os.environ['CODEX_MODEL_ID' if args.provider == 'codex' else 'BEDROCK_MODEL_ID'] = args.model
    return 'http://127.0.0.1:' + str(args.port)


def main():
    cli = parser()
    args = cli.parse_args()
    try:
        url = configure(args)
    except ValueError as exc:
        cli.error(str(exc))
    sys.path.insert(0, str(ROOT))
    import uvicorn
    print(f'Провайдер: {args.provider}. Откройте {url} ; полный план: {url}/plan', flush=True)
    print('Остановка: Ctrl+C. Проекты сохраняются. Переключение провайдера: остановить и запустить заново.', flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()
