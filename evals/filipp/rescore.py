"""Recompute atomic metrics from immutable saved trials; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
from evals.filipp.metrics import compare_facts,fact_count,summarize
from evals.filipp.acceptance import check_gate


def rescore(directory):
    metadata=json.loads((directory/'metadata.json').read_text())
    cases={x['id']:x for x in map(json.loads,(directory/'source/evals/filipp/data'/f"{metadata['split']}.jsonl").read_text().splitlines())}
    rows=[]
    for path in sorted(directory.glob('[dh][0-9][0-9]-[123].json')):
        row=json.loads(path.read_text())
        if row['status']!='blocked_q2' and row.get('expected_outcome')!='rejected_scope':
            draft=row['snapshots'][-1]['draft'];expected=row.get('expected_values',cases[row['scenario_id']]['expected_final'])
            compared=compare_facts(draft['values'],expected,draft['origins'])
            row.update(correct=compared['correct'],extracted=compared['extracted'],expected=fact_count(expected))
        rows.append(row)
    manifest=json.loads((Path(__file__).parent/'manifest.json').read_text())
    limits=manifest['thresholds'] or manifest.get('proposed_thresholds')
    report={'metric_version':'atomic-v2','source_run':str(directory),'summary':summarize(rows),
            'thresholds_approved':manifest['thresholds_approved'],'thresholds':limits,
            'gate':check_gate(rows,limits,expected_runs=len(cases)*3) if limits else None,
            'scorer_sha256':hashlib.sha256((Path(__file__).parent/'metrics.py').read_bytes()).hexdigest(),
            'note':'Результаты и статусы исходных запусков сохранены. Изменена только единица метрики: каждый элемент списка — отдельный атомарный факт. Дополнительных вызовов модели нет.'}
    target=directory/'atomic-report.json';target.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path);args=parser.parse_args()
    print(json.dumps(rescore(args.directory),ensure_ascii=False,indent=2))
