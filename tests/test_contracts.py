import copy
import json
from pathlib import Path
import pytest
from jsonschema.exceptions import ValidationError
from contracts.validate import validate_bundle, validate_stage

EXAMPLES = Path(__file__).resolve().parents[1]/'contracts/examples'
def load(name='completed.json'):
    return json.loads((EXAMPLES/name).read_text())
def payload(data,kind):
    return next(x['payload'] for x in data['artifacts'] if x['kind']==kind)

@pytest.mark.parametrize('name',['completed.json','over-budget.json'])
def test_examples(name):
    validate_bundle(load(name))

@pytest.mark.parametrize('name',['stage-ok.json','stage-needs-input.json','stage-failed.json'])
def test_stage_examples(name):
    validate_stage(load(name))

@pytest.mark.parametrize('fault', ['version','source','missing-line','duplicate-line','pack','price','unit','cycle','late-delivery','overlap','wait','total','budget','unknown-delivery','failed-audit','foreign-field','input-link'])
def test_rejects_inconsistent_plan(fault):
    d=load();w=payload(d,'work_plan');p=payload(d,'procurement');e=payload(d,'estimate');s=payload(d,'schedule')
    if fault=='version':d['artifacts'][2]['revision']=2
    elif fault=='source':e['lines'][0]['source_ids']=['missing']
    elif fault=='missing-line':e['lines'].pop()
    elif fault=='duplicate-line':e['lines'].append(copy.deepcopy(e['lines'][0]))
    elif fault=='pack':p['purchases'][1]['pack_count']=1
    elif fault=='price':p['purchases'][0]['unit_price_minor']=1
    elif fault=='unit':p['purchases'][0]['purchased_quantity']['unit']='m2'
    elif fault=='cycle':w['tasks'][0]['predecessor_ids']=['floor-task']
    elif fault=='late-delivery':p['deliveries'][0]['available_on']='2026-11-01'
    elif fault=='overlap':s['items'][1]['start_at']=s['items'][0]['start_at']
    elif fault=='wait':w['tasks'][1]['wait_after_hours']='40'
    elif fault=='total':e['total_minor']-=1
    elif fault=='budget':payload(d,'brief')['budget_minor']=1
    elif fault=='unknown-delivery':p['deliveries'][0]['amount_minor']=None
    elif fault=='failed-audit':payload(d,'audit')['status']='failed'
    elif fault=='foreign-field':d['artifacts'][0]['payload']['new_name']='oops'
    elif fault=='input-link':d['artifacts'][4]['input_artifact_ids']=[]
    with pytest.raises((ValueError,ValidationError)):
        validate_bundle(d)

def test_unknown_cost_remains_unresolved():
    d=load();d['status']='constraints_unresolved'
    payload(d,'procurement')['deliveries'][0]['amount_minor']=None
    e=payload(d,'estimate')
    next(x for x in e['lines'] if x['kind']=='delivery')['amount_minor']=None
    e.update(base_minor=None,reserve_minor=None,total_minor=None,budget_status='unknown')
    payload(d,'audit')['status']='incomplete'
    validate_bundle(d)

def test_over_budget_cannot_be_completed():
    d=load('over-budget.json');d['status']='completed'
    with pytest.raises(ValueError,match='статус'):
        validate_bundle(d)

def test_failure_cannot_return_success_document():
    d=load('stage-failed.json');d['artifact_id']='pretend-success'
    with pytest.raises(ValueError):validate_stage(d)

@pytest.mark.parametrize('fault', [None, 'stale', 'wrong-artifact', 'wrong-stage', 'wrong-input'])
def test_stage_output_matches_current_context(fault):
    from contracts.validate import validate_stage_output
    artifact = load()['artifacts'][0]
    result = {k: artifact[k] for k in ('schema_version','project_id','run_id','revision')}
    result.update(stage=artifact['kind'], status='ok', artifact_id=artifact['artifact_id'], questions=[], error=None)
    context = {k: result[k] for k in ('schema_version','project_id','run_id','revision','stage')}
    context['input_artifact_ids'] = artifact['input_artifact_ids'][:]
    if fault == 'stale': context['revision'] += 1
    elif fault == 'wrong-artifact': result['artifact_id'] = 'other'
    elif fault == 'wrong-stage': context['stage'] = 'estimate'
    elif fault == 'wrong-input': context['input_artifact_ids'] = ['other']
    if fault:
        with pytest.raises(ValueError): validate_stage_output(result, artifact, context)
    else:
        validate_stage_output(result, artifact, context)
