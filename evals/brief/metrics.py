from collections import defaultdict
from statistics import median

def summarize(rows):
    runs=len(rows)
    total=lambda key:sum(x.get(key,0) for x in rows)
    ratio=lambda a,b:a/b if b else None
    groups=defaultdict(list)
    for row in rows: groups[row['scenario_id']].append(row)
    stable=sum(len(v)==3 and all(x['status']=='passed' for x in v) for v in groups.values())
    times=[x['duration_s'] for x in rows if x['status']!='blocked_q2' and x.get('expected_outcome')!='rejected_scope']
    return {'runs':runs,'passed':sum(x['status']=='passed' for x in rows),
      'confirmed_runs':sum(x.get('actual_state')=='confirmed' for x in rows),
      'correct_refusals':sum(x.get('expected_outcome')=='rejected_scope' and x['status']=='passed' for x in rows),
      'failed':sum(x['status']=='failed' for x in rows),'blocked':sum(x['status'].startswith('blocked') for x in rows),
      'critical_count':total('critical_count'),'precision':ratio(total('correct'),total('extracted')),
      'recall':ratio(total('correct'),total('expected')),'stable_scenarios':stable,
      'scenario_count':len(groups),'stable_fraction':ratio(stable,len(groups)),
      'clarification_recall':ratio(total('questions_correct'),total('questions_expected')),
      'latency_n':len(times),'latency_median_s':median(times) if times else None,
      'latency_range_s':[min(times),max(times)] if times else None,
      'model_calls':total('model_calls'),'attempts':total('attempts')}


def atoms(values):
    import json
    result=set()
    for key,value in values.items():
        items=value if isinstance(value,list) else [value]
        for item in items:
            result.add((key,json.dumps(item,ensure_ascii=False,sort_keys=True)))
    return result


def fact_count(values):
    return len(atoms(values))


def compare_facts(values,expected,origins):
    fixed={'currency':'RUB','calendar.timezone':'Europe/Moscow','calendar.crew_count':1}
    unexpected=[k for k in values if k not in expected and origins.get(k)!='proposal' and not (k in fixed and values[k]==fixed[k])]
    extracted={k:v for k,v in values.items() if k in expected or k in unexpected}
    actual_atoms,expected_atoms=atoms(extracted),atoms(expected)
    return {'correct':len(actual_atoms & expected_atoms),'extracted':len(actual_atoms),'unexpected':unexpected}
