"""Design concepts and deterministic technology for one dry room. Contract 0.1.0."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
from jsonschema import Draft202012Validator, FormatChecker
from contracts.handoff import IDENTITY, call_stage
from app.brief.model import ModelRequest, ModelError
from app.design.scope import unsupported_exclusions, SCOPE_MESSAGE

ROOT = Path(__file__).resolve().parents[2]
SOURCE = {'id':'misha-tech-v1', 'kind':'synthetic',
          'reference':'docs/modules/design.md: учебная норма 0.2 л/м², резерв покрытия 10/15%; не строительная рекомендация',
          'captured_at':'2026-10-23T18:00:00+03:00'}
PROFILES = {'calm': {'floor_waste':'10', 'hours':'8', 'layout':'прямая укладка'},
            'accent': {'floor_waste':'15', 'hours':'12', 'layout':'диагональная укладка'}}

def payload_schema(kind):
    schema = json.loads((ROOT/'contracts/schemas/plan.schema.json').read_text())
    return next(x['properties']['payload'] for x in schema['properties']['artifacts']['items']['oneOf']
                if x['properties']['kind']['enum'] == [kind])

def doc(context, kind):
    return next(a for a in context['input_artifacts']+context['referenced_artifacts'] if a['kind']==kind)

def output(context, payload, assumptions=()):
    identity = {k:context[k] for k in IDENTITY}
    artifact = {**identity, 'kind':context['stage'], 'artifact_id':str(uuid4()),
                'input_artifact_ids':context['input_artifact_ids'], 'source_ids':[SOURCE['id']],
                'assumptions':list(assumptions), 'payload':deepcopy(payload)}
    return {**identity, 'stage':context['stage'], 'status':'ok', 'artifact_id':artifact['artifact_id'],
            'questions':[], 'error':None}, artifact

def failed(context, message, code='INVALID_INPUT'):
    return {**{k:context[k] for k in IDENTITY}, 'stage':context['stage'], 'status':'failed',
            'artifact_id':None,'questions':[], 'error':{'code':code,'message':message,
            'retryable':False,'field_paths':[]}}, None

async def generate_design(model, context):
    brief = doc(context,'brief')['payload']
    if unsupported_exclusions(brief):
        raise ModelError('unsupported_scope')
    instruction = ('Ты дизайнер учебного ремонта одной сухой комнаты: только покраска и ламинат. '
      'Предложи ровно две разные концепции: id calm (прямая укладка, запас пола 10%) и id accent '
      '(диагональная укладка, запас 15%, больше трудозатрат). Название, описание и цвета должны различаться. '
      'Не обещай конкретные цены, сертификаты, свойства товаров или изображения. Источник только misha-tech-v1. '
      'hard_preferences_met содержит только буквально выполнимые обязательные пожелания из брифа. '
      'Не объявляй выполненным то, что нельзя подтвердить учебным каталогом. Возвращай концепции по схеме.')
    schema = payload_schema('design_options')
    reply = await model.generate(ModelRequest(instruction, {'brief':brief, 'profiles':PROFILES}, schema, 120))
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(reply.data)
    concepts = reply.data['concepts']
    if len(concepts)!=2 or {x['id'] for x in concepts} != set(PROFILES):
        raise ModelError('invalid_response')
    if concepts[0]['name']==concepts[1]['name'] or concepts[0]['colors']==concepts[1]['colors']:
        raise ModelError('invalid_response')
    for concept in concepts:
        if not set(concept['hard_preferences_met']) <= set(brief['hard_preferences']):
            raise ModelError('invalid_response')
        if concept['source_ids'] != [SOURCE['id']]:
            raise ModelError('invalid_response')
    result, artifact = call_stage(lambda ctx: output(ctx, reply.data, ['Концепции текстовые; изображения не генерировались.']), context)
    return result, artifact, reply.metadata

def work_plan_handler():
    def handler(context):
        try:
            brief = doc(context,'brief')['payload']
            if unsupported_exclusions(brief):
                return failed(context, SCOPE_MESSAGE, 'UNSUPPORTED_REQUEST')
            selected = doc(context,'design_selection')['payload']['concept_id']
            concept = next(x for x in doc(context,'design_options')['payload']['concepts'] if x['id']==selected)
            profile = PROFILES[selected]
            room = brief['room']
            length,width,height,openings = (Decimal(room[k]) for k in ('length_m','width_m','height_m','openings_m2'))
            if not all(x.is_finite() for x in (length,width,height,openings)) or min(length,width,height)<=0 or openings<0:
                raise ValueError('Некорректная геометрия')
            walls = 2*(length+width)*height-openings
            floor = length*width
            if walls<=0: raise ValueError('Проёмы занимают всю площадь стен')
            if walls>60 or floor>25:
                return failed(context, 'Учебные тарифы поддерживают до 60 м² стен и 25 м² пола.', 'UNSUPPORTED_REQUEST')
            assumptions = ['Учебные нормы и фиксированные тарифы; не рыночная смета.',
                           'Краска: два слоя суммарно 0.2 л/м²; запас 10%.',
                           f"Выбрана {profile['layout']}; запас ламината {profile['floor_waste']}%."]
            unsupported=[]
            if room['surface_condition'] != 'prepared':
                assumptions.append('Основание требует обследования; выравнивание и устранение дефектов не рассчитаны.')
                unsupported.append('Проверка и подготовка неподтверждённого основания')
            missing = set(brief['hard_preferences'])-set(concept['hard_preferences_met'])
            if missing:
                assumptions.append('Концепция не подтверждает все обязательные пожелания: '+ '; '.join(sorted(missing)))
            refs=[SOURCE['id']]
            def qty(n,u): return {'value':format(n,'f') if isinstance(n,Decimal) else str(n),'unit':u}
            tasks=[{'id':'prepare','title':'Защита поверхностей и лёгкая подготовка','quantity':qty(1,'set'),
                    'predecessor_ids':[],'labor_hours':'8','wait_after_hours':'0','source_ids':refs},
                   {'id':'paint-task','title':'Окраска стен: '+concept['name'],'quantity':qty(walls,'m2'),
                    'predecessor_ids':['prepare'],'labor_hours':'8','wait_after_hours':'16','source_ids':refs},
                   {'id':'floor-task','title':'Ламинат: '+profile['layout'],'quantity':qty(floor,'m2'),
                    'predecessor_ids':['paint-task'],'labor_hours':profile['hours'],'wait_after_hours':'0','source_ids':refs}]
            def req(id,task,cat,n,u,w,key):
                return {'id':id,'task_ids':[task],'category':cat,'base_quantity':qty(n,u),
                        'waste_percent':w,'compatibility_key':key,'source_ids':refs}
            requirements=[req('prep-req','prepare','preparation',1,'set','0','dry-room'),
                          req('paint-req','paint-task','paint',walls*Decimal('0.2'),'l','10','prepared-wall'),
                          req('floor-req','floor-task','laminate',floor,'m2',profile['floor_waste'],'prepared-floor')]
            return output(context, {'tasks':tasks,'requirements':requirements,'unsupported_requests':unsupported}, assumptions)
        except (KeyError,StopIteration,ValueError,ArithmeticError):
            return failed(context, 'Проверьте бриф и выбор существующей концепции.')
    return handler
