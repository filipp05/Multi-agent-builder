from evals.brief.metrics import summarize

def test_metrics_keep_failures_and_empty_denominators():
    rows=[{'scenario_id':'a','status':'passed','critical_count':0,'correct':2,'extracted':2,'expected':2,'duration_s':1},
          {'scenario_id':'a','status':'failed','critical_count':1,'correct':1,'extracted':2,'expected':2,'duration_s':3},
          {'scenario_id':'a','status':'failed','critical_count':0,'correct':0,'extracted':0,'expected':2,'duration_s':5},
          {'scenario_id':'b','status':'blocked_q2','critical_count':0,'correct':0,'extracted':0,'expected':0,'duration_s':0}]
    m=summarize(rows)
    assert m['runs']==4 and m['failed']==2 and m['blocked']==1
    assert m['precision']==.75 and m['recall']==.5
    assert m['stable_scenarios']==0
    assert m['critical_count']==1
    assert summarize([])['precision'] is None

def test_fixed_contract_currency_is_not_a_hallucination():
    from evals.brief.metrics import compare_facts
    result=compare_facts({'budget_minor':20000000,'currency':'RUB'}, {'budget_minor':20000000}, {'budget_minor':'i','currency':'i'})
    assert result=={'correct':1,'extracted':1,'unexpected':[]}

def test_resume_stops_only_on_new_auth_failure():
    from evals.brief.run import should_stop, count_exec_attempts
    row={'calls':[{'status':'failed','error':'auth','metadata':{}}]}
    assert should_stop(row, newly_executed=True)
    assert not should_stop(row, newly_executed=False)
    assert count_exec_attempts(row['calls'])==0

def test_preference_items_are_individual_atomic_facts():
    from evals.brief.metrics import compare_facts, fact_count
    expected={'hard_preferences':['без запаха','светлый пол']}
    measured=compare_facts({'hard_preferences':['без запаха','дорогой пол']},expected,{'hard_preferences':'i'})
    assert measured['correct']==1
    assert measured['extracted']==2
    assert fact_count(expected)==2
