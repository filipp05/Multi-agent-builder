"""Partial requirements and the semantic gate before accepting a brief."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from app.brief.types import Extraction

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT/'contracts/schemas/plan.schema.json').read_text())
ARTIFACT_SCHEMA = CONTRACT['properties']['artifacts']['items']
# Derive the payload schema from the shared contract, do not maintain a second copy.
BRIEF_SCHEMA = next(x['properties']['payload'] for x in ARTIFACT_SCHEMA['oneOf'] if x['properties']['kind'].get('enum')==['brief']) if 'oneOf' in ARTIFACT_SCHEMA else None
if BRIEF_SCHEMA is None:
    BRIEF_SCHEMA = ARTIFACT_SCHEMA['items']  # unreachable for the current contract

LABELS = {'room.length_m':'Длина комнаты, м','room.width_m':'Ширина комнаты, м','room.height_m':'Высота комнаты, м',
 'room.openings_m2':'Общая площадь окон и дверей, м²','budget_minor':'Бюджет, руб.','region':'Город или регион',
 'start_date':'Дата начала (ГГГГ-ММ-ДД)','deadline':'Крайний срок (ГГГГ-ММ-ДД)'}
DEFAULTS = {'currency':'RUB','room.surface_condition':'unknown','hard_preferences':[],'soft_preferences':[], 'excluded_work':[],
 'calendar.timezone':'Europe/Moscow','calendar.work_weekdays':[0,1,2,3,4],'calendar.day_start_hour':9,'calendar.hours_per_day':8,'calendar.crew_count':1}


def empty_draft():
    return {'values':deepcopy(DEFAULTS),'origins':{k:'proposal' for k in DEFAULTS},'conflicts':{},'questions':[],
            'ambiguities':[],'unsupported_requests':[], 'ready':False}


def decimal_value(value):
    try:
        number=Decimal(str(value).replace(',','.'))
        if not number.is_finite(): raise ValueError('Нужно конечное число')
        return number
    except InvalidOperation:
        raise ValueError('Нужно число, например 2.7') from None


def normalize(path,value):
    if path in ('hard_preferences','soft_preferences','excluded_work','calendar.work_weekdays'):
        if isinstance(value,str): value=json.loads(value)
        if not isinstance(value,list): raise ValueError('Ожидается список')
        return value
    if path in ('budget_minor','calendar.day_start_hour','calendar.hours_per_day','calendar.crew_count'):
        if isinstance(value,bool): raise ValueError('Ожидается число')
        number=decimal_value(value)
        if not number.is_finite() or number != number.to_integral_value(): raise ValueError('Нужно целое число')
        return int(number)
    if path.startswith('room.') and path!='room.surface_condition':
        number=decimal_value(value)
        return format(number.normalize(),'f')
    if not isinstance(value,str) or not value.strip(): raise ValueError('Нужен непустой текст')
    return value.strip()


def with_form(draft,fields,input_id):
    draft=deepcopy(draft)
    for key,value in fields.items():
        draft['values'][key]=normalize(key,value)
        draft['origins'][key]=input_id
        draft['conflicts'].pop(key,None)
    return draft


def merge_extraction(draft, extraction: Extraction, inputs):
    draft=deepcopy(draft)
    records={x['id']:x for x in inputs}
    positions={x['id']:n for n,x in enumerate(inputs)}
    list_fields=('hard_preferences','soft_preferences','excluded_work')
    prepared=[]; groups={}
    for fact in extraction.candidates:
        source=records.get(fact.input_id)
        if not source or fact.quote not in source['text']:
            raise ValueError('Модель вернула факт без подтверждающего фрагмента')
        path=fact.field_path
        if any(x.get('is_edit') and path in x.get('fields',{}) for x in inputs[positions[source['id']]+1:]):
            continue  # Explicit later form edits supersede earlier text facts for this field.
        value=normalize(path,fact.value)
        if path in list_fields:
            key=(source['id'],path)
            if key in groups:
                groups[key].extend(value)
                continue
            groups[key]=value
        prepared.append((source,path,value))
    for source,path,value in prepared:
        current=draft['values'].get(path)
        same_form=path in source.get('fields',{})
        explicit_answer=source.get('field_path')==path or (source.get('is_edit',False) and not same_form and path not in list_fields)
        if same_form and current!=value and draft['origins'].get(path)==source['id'] and (path not in list_fields or source.get('is_edit',False)):
            draft['conflicts'][path]={'previous':current,'candidate':value}
        elif path in list_fields and not explicit_answer:
            draft['values'][path]=list(dict.fromkeys((current or [])+value))
            draft['origins'][path]=source['id']
        elif explicit_answer:
            draft['values'][path]=value
            draft['origins'][path]=source['id']
            draft['conflicts'].pop(path,None)
        elif current is not None and current!=value and draft['origins'].get(path)!='proposal':
            draft['conflicts'][path]={'previous':current,'candidate':value}
        else:
            draft['values'][path]=value
            draft['origins'][path]=source['id']
    draft['ambiguities']=[x.model_dump() for x in extraction.ambiguities]
    draft['unsupported_requests']=list(dict.fromkeys(draft['unsupported_requests']+extraction.unsupported_requests))
    electrical_labels={'электрика','электромонтаж','электромонтажные работы','электропроводка'}
    ignored=draft.get('ignored_requests',[])
    pending=[]
    for request in draft['unsupported_requests']:
        if request.strip().casefold() in electrical_labels:
            ignored.append(request)
        else:
            pending.append(request)
    draft['ignored_requests']=list(dict.fromkeys(ignored))
    draft['unsupported_requests']=pending
    if ignored:
        draft['values']['excluded_work']=list(dict.fromkeys(draft['values']['excluded_work']+['Электрика']))
        draft['origins']['excluded_work']='scope_policy'
    return draft


def payload_from(draft):
    result={}
    for key,value in draft['values'].items():
        parts=key.split('.')
        if len(parts)==2: result.setdefault(parts[0],{})[parts[1]]=deepcopy(value)
        else: result[key]=deepcopy(value)
    return result


def validate_brief_payload(payload):
    errors=list(Draft202012Validator(BRIEF_SCHEMA,format_checker=FormatChecker()).iter_errors(payload))
    if errors:
        e=errors[0]
        raise ValueError('.'.join(str(x) for x in e.path)+': '+e.message)
    room=payload['room']
    length,width,height=(Decimal(room[k]) for k in ('length_m','width_m','height_m'))
    if min(length,width,height)<=0: raise ValueError('Размеры должны быть положительными')
    if Decimal(room['openings_m2'])>2*(length+width)*height: raise ValueError('Проёмы больше стен')
    if payload['deadline']<payload['start_date']: raise ValueError('Окончание раньше начала')
    cal=payload['calendar']
    if cal['day_start_hour']+cal['hours_per_day']>24: raise ValueError('Рабочий день выходит за полночь')


def format_field_value(path,value):
    if path=='calendar.day_start_hour':
        return f'{value:02d}:00' if type(value) is int and 0<=value<24 else str(value)+' — неверное время'
    if path=='budget_minor':
        return f'{Decimal(value)/100:,.2f}'.replace(',',' ').replace('.',',')+' ₽'
    if path.startswith('room.') and path!='room.surface_condition':
        return str(value).replace('.',',')+(' м²' if path=='room.openings_m2' else ' м')
    if path in ('start_date','deadline'):
        from datetime import date
        try:return date.fromisoformat(value).strftime('%d.%m.%Y')
        except (ValueError,TypeError):return str(value)+' — неверная дата'
    if path=='room.surface_condition':
        return {'unknown':'Неизвестно — требуется проверка','prepared':'Подготовлено','needs_preparation':'Нужна подготовка'}.get(value,str(value))
    if path=='calendar.work_weekdays':
        return ', '.join(['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][x] if type(x) is int and 0<=x<7 else str(x)+' — неверный день' for x in value)
    if path=='calendar.timezone':return 'Московское время' if value=='Europe/Moscow' else str(value)
    if path=='currency':return 'Рубли' if value=='RUB' else str(value)
    if isinstance(value,list):return '; '.join(map(str,value)) if value else 'Не указаны'
    return str(value)


def prepare_review(draft):
    draft=deepcopy(draft)
    questions=[]
    for key,label in LABELS.items():
        if key not in draft['values']:
            message='Укажите: '+label
            if key=='budget_minor':message='Какой бюджет на работы и материалы, в рублях?'
            elif key.startswith('room.'):message+=' (можно с запятой, например 2,7)'
            elif key in ('start_date','deadline'):message+='; например 2026-12-01'
            questions.append((key,message))
    for key,conflict in draft['conflicts'].items():
        label=LABELS.get(key,key).split(',')[0].split(' (')[0]
        questions.append((key,f"{label}: указаны {format_field_value(key,conflict['previous'])} и {format_field_value(key,conflict['candidate'])}. Какое значение верно?"))
    for ambiguity in draft['ambiguities']:
        if isinstance(ambiguity,str):
            questions.append(('clarification',ambiguity))
        elif not (ambiguity['kind']=='value_conflict' and ambiguity['field_path'] in draft['conflicts']):
            questions.append(('clarification',ambiguity['message']))
    if not questions:
        try: validate_brief_payload(payload_from(draft))
        except ValueError as e: questions.append(('correction',str(e)+' — исправьте исходные данные.'))
    draft['questions']=[{'id':str(uuid4()),'field_path':key,'message':text} for key,text in questions]
    draft['ready']=not questions and not draft['unsupported_requests']
    return draft
