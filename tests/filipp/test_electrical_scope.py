from fastapi.testclient import TestClient
from app.filipp.api import create_app
from app.filipp.model import ModelReply
from test_full_brief import FIELDS, create, wait, command


class ElectricalModel:
    async def generate(self, request):
        return ModelReply({'candidates': [], 'ambiguities': [],
                           'unsupported_requests': ['электрика']}, {})


def test_electrical_exclusion_survives_confirmation_edit_and_reload(tmp_path):
    db = tmp_path / 'db'
    with TestClient(create_app(db, ElectricalModel())) as c:
        s = create(c, text='Покраска и электрика')
        assert s['status'] == 'ready_to_confirm'
        assert 'Электрика' in s['draft']['values']['excluded_work']
        assert 'не можем реализовать' in ' '.join(s['warnings'])
        accepted = command(c, s, 'confirm').json()
        assert 'Электрика' in accepted['artifact']['payload']['excluded_work']
        command(c, accepted, 'edit', input={'text': 'Меняю регион', 'fields': {'region': 'Тула', 'excluded_work': []}})
        edited = wait(c, s['project_id'])
        assert edited['status'] == 'ready_to_confirm'
        assert 'Электрика' in edited['draft']['values']['excluded_work']
    with TestClient(create_app(db, ElectricalModel())) as c:
        restored = c.get('/api/projects/' + s['project_id']).json()
        assert restored['warnings']
        assert restored['inputs'][0]['text'] == 'Покраска и электрика'


def test_electrical_exclusion_does_not_skip_missing_dimensions(tmp_path):
    with TestClient(create_app(tmp_path/'db', ElectricalModel())) as c:
        s = create(c, {k:v for k,v in FIELDS.items() if k != 'room.height_m'})
        assert s['status'] == 'waiting_input'
        assert any(q['field_path'] == 'room.height_m' for q in s['draft']['questions'])
        assert command(c, s, 'confirm').status_code == 409


def test_other_unsupported_work_still_blocks(tmp_path):
    class Mixed(ElectricalModel):
        async def generate(self, request):
            return ModelReply({'candidates': [], 'ambiguities': [],
                               'unsupported_requests': ['электрика', 'сантехника']}, {})
    with TestClient(create_app(tmp_path/'db', Mixed())) as c:
        s = create(c)
        assert s['status'] == 'blocked_scope'
        assert s['draft']['unsupported_requests'] == ['сантехника']
        assert s['warnings']
        assert command(c, s, 'confirm').status_code == 409
