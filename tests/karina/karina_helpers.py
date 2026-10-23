from copy import deepcopy

from app.filipp.model import ModelReply
from app.karina import service
from contracts.handoff import make_context


def bundle():
    return service.example_bundle()


def payload(data, kind):
    return next(a['payload'] for a in data['artifacts'] if a['kind'] == kind)


def schedule_context(data=None):
    data = deepcopy(data or bundle())
    return make_context(data, 'schedule', [a for a in data['artifacts'] if a['kind'] not in ('schedule', 'audit')], data['sources'], data['offers'])


class FakeModel:
    def __init__(self, findings=()):
        self.requests = []
        self.findings = list(findings)

    async def generate(self, request):
        self.requests.append(request)
        if 'calendar_notes' in request.schema['properties']:
            data = {'summary': 'Учебный график рассчитан кодом; срок проверен.',
                    'calendar_notes': ['Подтверждённый рабочий календарь сохранён.'],
                    'task_explanations': [{'task_id': s['task_id'], 'text': f'Работа {s["task_id"]} после предшественников и поставок.'} for s in request.data['schedule']['items']]}
        else:
            data = {'summary': 'Смысловая проверка на явной тестовой замене.', 'findings': deepcopy(self.findings)}
        return ModelReply(data, {'provider': 'explicit_test_double', 'model': 'fake', 'attempt': 1})
