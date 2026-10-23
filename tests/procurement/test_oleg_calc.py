from decimal import Decimal

import pytest

from app.procurement import calc

ROOM = {'length_m': '4', 'width_m': '5', 'height_m': '2.7', 'openings_m2': '4'}


def test_reference_room_walls_paint_and_floor():
    walls = calc.wall_area(ROOM)
    assert walls == Decimal('44.6')
    paint = calc.paint_litres(walls, 2, Decimal('0.1'))
    assert paint == Decimal('8.92')
    assert calc.with_waste(paint, Decimal('10')) == Decimal('9.812')
    assert calc.pack_count(paint, Decimal('10'), Decimal('5')) == 2
    assert calc.pack_count(Decimal('20'), Decimal('10'), Decimal('2.2')) == 10


def test_packs_round_up_never_down():
    assert calc.pack_count(Decimal('20.01'), Decimal('10'), Decimal('2.2')) == 11
    assert calc.pack_count(Decimal('8.92'), Decimal('10'), Decimal('2.5')) == 4


def test_waste_is_applied_once_and_empty_pack_rejected():
    assert calc.pack_count(Decimal('1'), Decimal('0'), Decimal('1')) == 1
    with pytest.raises(ValueError, match='Пустая'):
        calc.pack_count(Decimal('1'), Decimal('0'), Decimal('0'))


def test_openings_larger_than_walls_rejected():
    with pytest.raises(ValueError, match='Проёмы'):
        calc.wall_area({**ROOM, 'openings_m2': '49'})


def test_reserve_rounds_half_up_on_whole_base():
    assert calc.reserve_minor(6750000, '10') == 675000
    assert calc.reserve_minor(5, '10') == 1
    assert calc.reserve_minor(4, '10') == 0


def test_unknown_amount_makes_all_totals_unknown():
    lines = [{'amount_minor': 100}, {'amount_minor': None}]
    assert calc.totals(lines, '10', 1000) == {'base_minor': None, 'reserve_percent': '10', 'reserve_minor': None,
                                              'total_minor': None, 'budget_status': 'unknown'}


def test_budget_status_boundaries():
    lines = [{'amount_minor': 6750000}]
    assert calc.totals(lines, '10', 8000000)['budget_status'] == 'satisfied'
    assert calc.totals(lines, '10', 7425000)['budget_status'] == 'satisfied'
    assert calc.totals(lines, '10', 7000000)['budget_status'] == 'violated'
