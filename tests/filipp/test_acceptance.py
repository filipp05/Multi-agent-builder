from evals.filipp.acceptance import check_gate

def test_blocked_cases_are_visible_and_never_passed():
    rows=[{'scenario_id':f'd{x//3}','status':'passed','critical_count':0,'correct':1,'expected':1,'extracted':1,'duration_s':2,'questions_correct':0,'questions_expected':0} for x in range(27)]
    rows += [{'scenario_id':'q2','status':'blocked_q2','critical_count':0,'correct':0,'expected':0,'extracted':0,'duration_s':2} for _ in range(3)]
    limits={'precision':.98,'recall':.98,'clarification_recall':1,'success_rate':.95,'stable_fraction':.9,'latency_p95_s':45}
    report=check_gate(rows,limits,expected_runs=30)
    assert report['supported_success_rate']==1
    assert report['supported_stable_fraction']==1
    assert report['blocked_runs']==3
    assert not report['complete_acceptance']
    assert report['supported_gate_passed']
    rows[0]['critical_count']=1
    assert not check_gate(rows,limits,expected_runs=30)['supported_gate_passed']


def test_correct_refusal_is_not_a_successful_supported_brief():
    supported=[{'scenario_id':f'd{x//3}','repeat':x%3+1,'status':'passed','expected_outcome':'confirmed','critical_count':0,'correct':1,'expected':1,'extracted':1,'duration_s':2} for x in range(27)]
    refused=[{'scenario_id':'unsupported','repeat':x+1,'status':'passed','expected_outcome':'rejected_scope','actual_state':'blocked_scope','critical_count':0} for x in range(3)]
    limits={'precision':.98,'recall':.98,'success_rate':.95,'stable_fraction':.9,'latency_p95_s':45}
    r=check_gate(supported+refused,limits,expected_runs=30)
    assert r['supported_runs']==27 and r['correct_refusals']==3
    assert r['complete_acceptance']
    refused[0]['status']='failed';refused[0]['actual_state']='failed'
    assert not check_gate(supported+refused,limits,expected_runs=30)['complete_acceptance']
    assert not check_gate(supported+refused[1:]+refused[1:2],limits,expected_runs=30)['complete_acceptance']
