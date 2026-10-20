"""Проверяемый обмен этапов. Демонстрация использует готовые данные, без LLM."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Callable

from contracts.validate import DEPENDENCIES, index, require, validate_bundle, validate_stage_output

IDENTITY = ('schema_version', 'project_id', 'run_id', 'revision')
# Единый интерфейс: обработчик(context) -> (ответ этапа, документ или None).
StageHandler = Callable[[dict], tuple[dict, dict | None]]


def make_context(identity, stage, accepted, sources, offers, *, user_request=None, selected_concept_id=None):
    """Входы берутся только из уже принятых документов текущего запуска."""
    require(stage in DEPENDENCIES, 'Неизвестный этап')
    docs = index(accepted, 'artifact_id')
    kinds = index(accepted, 'kind')
    require(DEPENDENCIES[stage] <= kinds.keys(), 'Ещё не готовы входы этапа')
    direct = [kinds[k]['artifact_id'] for k in sorted(DEPENDENCIES[stage])]
    visible = set()

    def visit(doc_id):
        require(doc_id in docs, 'Не найден входной документ')
        if doc_id in visible:
            return
        visible.add(doc_id)
        doc = docs[doc_id]
        require(all(doc[k] == identity[k] for k in IDENTITY), 'Вход относится к другому запуску')
        for parent in doc['input_artifact_ids']:
            visit(parent)

    for doc_id in direct:
        visit(doc_id)
    context = {k: identity[k] for k in IDENTITY}
    context.update(stage=stage, input_artifact_ids=direct,
                   input_artifacts=[docs[x] for x in direct],
                   referenced_artifacts=[docs[x] for x in sorted(visible-set(direct))],
                   sources=sources, offers=offers,
                   user_request=None, selected_concept_id=None)
    if stage == 'brief':
        require(isinstance(user_request, str) and bool(user_request.strip()), 'Нужен запрос пользователя')
        context['user_request'] = user_request
    if stage == 'design_selection':
        concepts = kinds['design_options']['payload']['concepts']
        require(selected_concept_id in {c['id'] for c in concepts}, 'Нужен выбор существующей концепции')
        context['selected_concept_id'] = selected_concept_id
    return deepcopy(context)


def call_stage(handler: StageHandler, context: dict):
    """Обработчик не может менять контекст, по которому проверяется его ответ."""
    result, artifact = handler(deepcopy(context))
    validate_stage_output(result, artifact, context)
    return deepcopy(result), deepcopy(artifact)


def verify_handoffs(bundle):
    """Проверить все восемь передач на записанном примере; не запуск агентов."""
    validate_bundle(bundle)
    by_kind = index(bundle['artifacts'], 'kind')
    accepted, trace = [], []
    selected = by_kind['design_selection']['payload']['concept_id']
    for stage in DEPENDENCIES:
        context = make_context(bundle, stage, accepted, bundle['sources'], bundle['offers'],
                               user_request='Учебный пример ремонта комнаты', selected_concept_id=selected)
        artifact = by_kind[stage]
        result = {k: bundle[k] for k in IDENTITY}
        result.update(stage=stage, status='ok', artifact_id=artifact['artifact_id'], questions=[], error=None)
        _, checked = call_stage(lambda _: (result, artifact), context)
        accepted.append(checked)
        trace.append({'stage': stage, 'inputs': context['input_artifact_ids'], 'output': checked['artifact_id']})
    rebuilt = {**bundle, 'artifacts': accepted}
    validate_bundle(rebuilt)
    return trace


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    for row in verify_handoffs(json.loads(args.file.read_text())):
        print(f"{row['stage']}: {', '.join(row['inputs']) or 'запрос пользователя'} → {row['output']}")
    print('Восемь передач проверены на готовом примере. Агенты и LLM не запускались.')
