"""Shared schema access and input errors; no procurement or scheduling calculations."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from contracts.handoff import IDENTITY
from contracts.validate import DEPENDENCIES

ROOT = Path(__file__).resolve().parents[2]


class InputError(ValueError):
    def __init__(self, message, path='', code='INVALID_INPUT'):
        super().__init__(message)
        self.paths = [path] if path else []
        self.code = code


def require(condition, message, path='', code='INVALID_INPUT'):
    if not condition:
        raise InputError(message, path, code)


@lru_cache
def schema():
    return json.loads((ROOT/'contracts/schemas/plan.schema.json').read_text())


def schema_errors(value, section):
    item = schema()['properties'][section]['items']
    return Draft202012Validator(item, format_checker=FormatChecker()).iter_errors(value)


def unique(items, key='id', path=''):
    result = {}
    for item in items:
        require(item[key] not in result, f'Повтор идентификатора: {item[key]}', path)
        result[item[key]] = item
    return result


def documents(context):
    return context['input_artifacts'] + context['referenced_artifacts']


def validate_context(context, stage, *, inspect_artifacts=True):
    require(context['stage'] == stage, f'Ожидался этап {stage}', 'stage')
    identity_schema = {'type': 'object', 'properties': {
        k: schema()['properties'][k] for k in IDENTITY}, 'required': list(IDENTITY)}
    error = next(Draft202012Validator(identity_schema).iter_errors(context), None)
    require(error is None, 'Некорректные идентификаторы запуска', 'identity')
    direct = context['input_artifacts']
    require({a['kind'] for a in direct} == DEPENDENCIES[stage],
            'Нужны все прямые входные документы этапа', 'input_artifacts')
    require(len(direct) == len(DEPENDENCIES[stage]), 'Повтор вида входного документа', 'input_artifacts')
    require(set(context['input_artifact_ids']) == {a['artifact_id'] for a in direct},
            'Идентификаторы входов не совпадают с документами', 'input_artifact_ids')
    if not inspect_artifacts:
        return
    docs = unique(documents(context), 'artifact_id', 'artifacts')
    unique(list(docs.values()), 'kind', 'artifacts')
    for doc in docs.values():
        error = next(schema_errors(doc, 'artifacts'), None)
        require(error is None, f'Некорректный документ {doc.get("kind")}: {error.message if error else ""}', 'artifacts')
        require(all(doc[k] == context[k] for k in IDENTITY),
                'Вход относится к другому запуску или версии', 'artifacts', 'STALE_REVISION')
        refs = doc['input_artifact_ids']
        require(len(refs) == len(set(refs)) and set(refs) <= docs.keys(),
                'Некорректные ссылки документа', 'input_artifact_ids')
        require({docs[x]['kind'] for x in refs} == DEPENDENCIES[doc['kind']],
                'Неверные зависимости входного документа', 'input_artifact_ids')
    for section in ('sources', 'offers'):
        unique(context[section], path=section)
        for item in context[section]:
            error = next(schema_errors(item, section), None)
            require(error is None, f'Некорректные данные: {section}', section)
    source_ids = {s['id'] for s in context['sources']}

    def check_sources(value):
        if isinstance(value, dict):
            require(set(value.get('source_ids', [])) <= source_ids, 'Неизвестный источник', 'source_ids')
            for child in value.values():
                check_sources(child)
        elif isinstance(value, list):
            for child in value:
                check_sources(child)
    check_sources(list(docs.values()) + context['offers'])


def payloads(context):
    return {a['kind']: a['payload'] for a in documents(context)}


def artifact(context, kind, payload, artifact_id, assumptions=(), source_ids=()):
    return {**{k: context[k] for k in IDENTITY}, 'kind': kind, 'artifact_id': artifact_id,
            'input_artifact_ids': list(context['input_artifact_ids']), 'source_ids': sorted(set(source_ids)),
            'assumptions': list(assumptions), 'payload': deepcopy(payload)}
