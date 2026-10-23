"""Two isolated model contexts; models may explain and flag, never set dates or erase findings."""
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from app.brief.model import ModelError, ModelRequest
from app.planning.common import documents, schema

PROMPTS = Path(__file__).parent/'prompts'
PLANNER_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['summary', 'calendar_notes', 'task_explanations'],
    'properties': {
        'summary': {'type': 'string', 'minLength': 1},
        'calendar_notes': {'type': 'array', 'items': {'type': 'string', 'minLength': 1}},
        'task_explanations': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['task_id', 'text'],
            'properties': {'task_id': {'type': 'string'}, 'text': {'type': 'string', 'minLength': 1}}}}}}
FINDING_SCHEMA = deepcopy(next(a['properties']['payload']['properties']['findings']['items']
    for a in schema()['properties']['artifacts']['items']['oneOf'] if a['properties']['kind']['enum'] == ['audit']))
FINDING_SCHEMA['properties'].pop('id')
FINDING_SCHEMA['required'].remove('id')
REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['summary', 'findings'],
                'properties': {'summary': {'type': 'string', 'minLength': 1},
                               'findings': {'type': 'array', 'items': FINDING_SCHEMA, 'maxItems': 30}}}


def validate_reply(reply, expected_schema):
    if next(Draft202012Validator(expected_schema, format_checker=FormatChecker()).iter_errors(reply.data), None):
        raise ModelError('invalid_response', {'stage': 'karina_response_schema'})


async def explain_schedule(model, context, schedule, details, wishes=''):
    data = {'brief': next(a['payload'] for a in documents(context) if a['kind'] == 'brief'),
            'calendar_wishes': wishes, 'schedule': schedule['payload'], 'calculation_details': details,
            'assumptions': schedule['assumptions']}
    reply = await model.generate(ModelRequest((PROMPTS/'planner.txt').read_text(), data, PLANNER_SCHEMA, 120))
    validate_reply(reply, PLANNER_SCHEMA)
    expected = {x['task_id'] for x in schedule['payload']['items']}
    actual = [x['task_id'] for x in reply.data['task_explanations']]
    if set(actual) != expected or len(actual) != len(expected):
        raise ModelError('invalid_response', {'stage': 'planner_task_references'})
    return reply.data, reply.metadata


async def review_bundle(model, context, rules):
    # No old audit, planner explanation, smetchik conversation or self-evaluation.
    data = {'artifacts': documents(context), 'sources': context['sources'],
            'offers': context['offers'], 'control_rules': rules}
    reply = await model.generate(ModelRequest((PROMPTS/'controller.txt').read_text(), data, REVIEW_SCHEMA, 120))
    validate_reply(reply, REVIEW_SCHEMA)
    validate_findings(reply.data['findings'], context)
    for finding in reply.data['findings']:
        finding['code'] = 'LLM_'+finding['code']
    return reply.data, reply.metadata


def validate_findings(findings, context):
    if next(Draft202012Validator({'type': 'array', 'items': FINDING_SCHEMA, 'maxItems': 30}).iter_errors(findings), None):
        raise ModelError('invalid_response', {'stage': 'controller_finding_schema'})
    docs = {a['artifact_id']: a for a in documents(context)}
    sources = {s['id'] for s in context['sources']}
    item_ids = set()

    def collect(value):
        if isinstance(value, dict):
            if isinstance(value.get('id'), str):
                item_ids.add(value['id'])
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    # Findings may cite actual catalogue items (e.g. paint properties), not only document rows.
    collect(list(docs.values()) + context['offers'])
    for finding in findings:
        if not set(finding['artifact_ids']) <= docs.keys() or not set(finding['source_ids']) <= sources or not set(finding['item_ids']) <= item_ids:
            raise ModelError('invalid_response', {'stage': 'controller_finding_references'})
