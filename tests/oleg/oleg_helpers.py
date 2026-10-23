"""Общие данные тестов Олега. FakeModel — явная тестовая замена модели, не результат настоящего вызова."""
import json
from copy import deepcopy

from app.filipp.model import ModelError, ModelReply
from app.oleg import service
from app.oleg.stages import load
from contracts.handoff import make_context

RATES = load('rates.json')
EXAMPLE_CHOICES = {'prep-req': 'primer', 'paint-req': 'paint', 'floor-req': 'floor'}


def raw_example():
    return json.loads(service.EXAMPLE.read_text(encoding='utf-8'))


def bundle():
    return service.example_bundle()


def upstream(data):
    return [a for a in data['artifacts'] if a['kind'] in service.UPSTREAM]


def procurement_context(data=None, sources=None):
    data = data or bundle()
    return make_context(data, 'procurement', upstream(data), data['sources']+RATES['sources'] if sources is None else sources, data['offers'])


def estimate_context(procurement, data=None):
    data = data or bundle()
    return make_context(data, 'estimate', upstream(data)+[procurement], data['sources']+RATES['sources'], data['offers'])


def brief_of(data):
    return next(a for a in data['artifacts'] if a['kind'] == 'brief')['payload']


class FakeModel:
    """Тестовая замена: возвращает заданный выбор и пояснения ко всем строкам, которые ей передали."""
    def __init__(self, choices=None, *, explain_all=True, error=None):
        self.choices, self.explain_all, self.error, self.requests = choices or dict(EXAMPLE_CHOICES), explain_all, error, []

    async def generate(self, request):
        self.requests.append(deepcopy(request))
        if self.error:
            raise ModelError(self.error)
        if 'choices' in request.schema['properties']:
            data = {'choices': [{'requirement_id': r, 'offer_id': o, 'reason': f'тест: {o}'} for r, o in self.choices.items()]}
        else:
            lines = request.data['lines'] if self.explain_all else request.data['lines'][1:]
            data = {'summary': 'тестовая сводка', 'explanations': [{'line_id': x['id'], 'text': f"тест: {x['kind']}"} for x in lines]}
        return ModelReply(data, {'provider': 'fake', 'model': 'test-double'})
