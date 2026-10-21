from copy import deepcopy
import json
from pathlib import Path
import pytest
from app.filipp.brief import validate_brief_payload

@pytest.mark.parametrize('path,value,reason',[
 ('room.length_m','0','Размеры'),('room.openings_m2','10000','Проёмы'),
 ('deadline','2000-01-01','Окончание'),('calendar.day_start_hour',23,'полночь'),
 ('budget_minor',0,'budget_minor'),('start_date','2026-02-30','date')])
def test_invalid_brief(path,value,reason):
    bundle=json.loads(Path('contracts/examples/completed.json').read_text())
    p=next(x['payload'] for x in bundle['artifacts'] if x['kind']=='brief')
    parts=path.split('.')
    target=p
    for key in parts[:-1]: target=target[key]
    target[parts[-1]]=value
    with pytest.raises(ValueError,match=reason): validate_brief_payload(p)
