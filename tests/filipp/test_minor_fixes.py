from copy import deepcopy
import pytest
from app.filipp.brief import empty_draft, prepare_review, merge_extraction, with_form
from app.filipp.types import Extraction
from app.filipp.presentation import present
from test_full_brief import FIELDS


def test_typed_conflict_deduplicated_but_semantic_question_preserved():
    d=with_form(empty_draft(),FIELDS,'form')
    ex=Extraction.model_validate({'candidates':[{'field_path':'budget_minor','value':'10000000','input_id':'i','quote':'100000'}],
        'ambiguities':[{'kind':'value_conflict','field_path':'budget_minor','message':'Какой бюджет?'},
                       {'kind':'semantic','field_path':'deadline','message':'Первое декабря включительно?'}], 'unsupported_requests':[]})
    d=prepare_review(merge_extraction(d,ex,[{'id':'i','text':'100000'}]))
    assert len(d['questions'])==2
    assert d['questions'][0]['message']=='Бюджет: указаны 200 000,00 ₽ и 100 000,00 ₽. Какое значение верно?'
    assert d['questions'][1]['field_path']=='clarification'
    assert d['questions'][1]['message']=='Первое декабря включительно?'


def test_legacy_and_unmatched_ambiguities_not_lost():
    d=with_form(empty_draft(),FIELDS,'form')
    d['ambiguities']=['Старый вопрос',{'kind':'value_conflict','field_path':'deadline','message':'Какая дата?'}]
    assert [q['message'] for q in prepare_review(d)['questions']]==['Старый вопрос','Какая дата?']


def test_formatting_exact_money_dimensions_and_dates():
    from app.filipp.brief import format_field_value
    assert format_field_value('budget_minor',12345)=='123,45 ₽'
    assert format_field_value('room.height_m','2.7')=='2,7 м'
    assert format_field_value('room.openings_m2','3.25')=='3,25 м²'
    assert format_field_value('deadline','2026-12-01')=='01.12.2026'


def test_invalid_ambiguity_path_and_missing_conflict_path_rejected():
    for field in ['imaginary',None]:
        with pytest.raises(ValueError):
            Extraction.model_validate({'candidates':[],'ambiguities':[{'kind':'value_conflict','field_path':field,'message':'?'}],'unsupported_requests':[]})


def test_conflicted_review_and_unsupported_next_step():
    d=with_form(empty_draft(),FIELDS,'form')
    d['conflicts']={'room.width_m':{'previous':'4','candidate':'3'}}
    d['unsupported_requests']=['сантехника <script>']
    s=present({'status':'blocked_scope','draft':d,'error':None})
    row=next(r for r in s['review'] if r['field']=='room.width_m')
    assert row['conflicted']
    assert '4 м' in row['display_value'] and '3 м' in row['display_value']
    assert 'сантехника <script>' in ' '.join(s['warnings'])
    assert 'новый проект' in s['next_action'].lower()
    assert 'сухой комнате' in s['scope_notice']


def test_two_conflicts_and_semantic_on_same_field_are_preserved():
    d=with_form(empty_draft(),FIELDS,'form')
    d['conflicts']={'budget_minor':{'previous':20000000,'candidate':10000000},'deadline':{'previous':'2026-12-01','candidate':'2026-12-02'}}
    d['ambiguities']=[{'kind':'semantic','field_path':'deadline','message':'Включительно?'}]
    assert len(prepare_review(d)['questions'])==3


def test_invalid_date_and_weekday_can_be_displayed_for_correction(tmp_path):
    from fastapi.testclient import TestClient
    from app.filipp.api import create_app
    from test_full_brief import EmptyModel,create,command
    with TestClient(create_app(tmp_path/'db',EmptyModel())) as c:
        s=create(c,{**FIELDS,'deadline':'not-a-date','calendar.work_weekdays':[8]})
        assert s['status']=='waiting_input'
        assert s['draft']['questions']
        assert command(c,s,'confirm').status_code==409
