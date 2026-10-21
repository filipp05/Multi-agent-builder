from uuid import uuid4
from contracts.handoff import make_context,call_stage
from app.filipp.brief import payload_from,validate_brief_payload

def build_confirmed_brief(snapshot):
    payload=payload_from(snapshot['draft'])
    validate_brief_payload(payload)
    identity={k:snapshot[k] for k in ('project_id','run_id','revision')}
    identity['schema_version']='0.1.0'
    context=make_context(identity,'brief',[],[],[],user_request=snapshot['inputs'][0]['text'])
    artifact={**identity,'artifact_id':str(uuid4()),'kind':'brief','input_artifact_ids':[],
              'source_ids':[],'assumptions':[], 'payload':payload}
    if payload['room']['surface_condition']=='unknown':
        artifact['assumptions'].append('Состояние основания неизвестно; окончательный план требует проверки основания.')
    result={**identity,'stage':'brief','status':'ok','artifact_id':artifact['artifact_id'],'questions':[],'error':None}
    return call_stage(lambda _: (result,artifact),context)
