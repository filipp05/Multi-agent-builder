"""Acceptance gates keep unresolved scope separate from measured supported cases."""
from collections import defaultdict
from math import ceil
from evals.filipp.metrics import summarize

def check_gate(rows,limits,*,expected_runs):
    supported=[x for x in rows if not x['status'].startswith('blocked') and x.get('expected_outcome')!='rejected_scope']
    refusals=[x for x in rows if x.get('expected_outcome')=='rejected_scope']
    blocked=sum(x['status'].startswith('blocked') for x in rows)
    correct_refusals=sum(x['status']=='passed' and x.get('actual_state')=='blocked_scope' for x in refusals)
    m=summarize(supported)
    groups=defaultdict(list)
    for row in supported:groups[row['scenario_id']].append(row)
    stable=sum(len(v)==3 and all(x['status']=='passed' for x in v) for v in groups.values())
    success=m['passed']/len(supported) if supported else None
    consistency=stable/len(groups) if groups else None
    times=sorted(x['duration_s'] for x in supported)
    p95=times[ceil(.95*len(times))-1] if len(times)>=20 else None
    measured={'precision':m['precision'],'recall':m['recall'],'clarification_recall':m['clarification_recall'],
              'success_rate':success,'stable_fraction':consistency,'latency_p95_s':p95}
    failures=[]
    if len(rows)!=expected_runs:failures.append('incomplete_run_count')
    if rows and all('repeat' in x for x in rows) and len({(x['scenario_id'],x['repeat']) for x in rows})!=len(rows):failures.append('duplicate_trials')
    if correct_refusals!=len(refusals):failures.append('incorrect_refusal')
    if sum(x['critical_count'] for x in rows):failures.append('critical_errors')
    for key,threshold in limits.items():
        value=measured[key]
        if key=='clarification_recall' and value is None:continue
        if value is None or (value>threshold if key=='latency_p95_s' else value<threshold):failures.append(key)
    return {'supported_success_rate':success,'supported_stable_fraction':consistency,
            'correct_refusals':correct_refusals,'expected_refusals':len(refusals),'supported_runs':len(supported),'blocked_runs':blocked,'latency_n':len(times),
            'measured':measured,'failures':failures,'supported_gate_passed':not failures,
            'complete_acceptance':not failures and not blocked}
