"""Explicit live probe, never run by pytest."""
import asyncio
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.filipp.model import CodexModel, ModelRequest, ModelError, run_process

async def main():
    diagnostics = []
    async def runner(argv, **kwargs):
        code, out, err = await run_process(argv, **kwargs)
        if code:
            # CLI errors only, omit event content and all known credentials.
            import os
            text = err.decode(errors='replace')
            for key, value in os.environ.items():
                if len(value) > 5 and any(x in key.upper() for x in ('TOKEN','SECRET','PASSWORD','API_KEY')):
                    text = text.replace(value, '[redacted]')
            text = re.sub(r'(?:sk-|y[01]_|t[01]_|AQAD-)[\w.-]+', '[redacted]', text)
            diagnostics.append(text[:2000])
        return code, out, err
    schema = {'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}
    try:
        result = await CodexModel(runner=runner).generate(ModelRequest('Return {"ok":true}. Do not call any tools.', {}, schema, 45))
        report = {'status':'ok', 'result':result.data, 'metadata':result.metadata}
        if '--extraction' in sys.argv:
            root=Path('app/filipp')
            full = await CodexModel(runner=runner).generate(ModelRequest((root/'prompts/brief.txt').read_text(),
                {'inputs':[{'id':'smoke','text':'Комната 5 на 4 метра, высота 2,7 м. Бюджет 200 тысяч рублей. Москва. Покрасить стены и положить ламинат.', 'field_path':None, 'received_at':'2026-10-21T12:00:00+03:00','fields':{}}], 'current_values':{}},
                json.loads((root/'schemas/extraction.schema.json').read_text()), 60))
            report['extraction']={'data':full.data,'metadata':full.metadata}

    except ModelError as e:
        report = {'status':'failed', 'error':e.code, 'diagnostics':diagnostics}
    path = Path('.scratch/filipp-mvp/results/smoke.json')
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='ok' else 1

if __name__=='__main__': sys.exit(asyncio.run(main()))
