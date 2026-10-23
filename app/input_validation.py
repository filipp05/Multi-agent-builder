"""Validate collection shapes before stage code inspects individual documents."""


def require_object_lists(bundle):
    if not isinstance(bundle, dict):
        raise ValueError('Комплект должен быть объектом')
    for name in ('artifacts', 'sources', 'offers'):
        if name not in bundle:
            raise ValueError(f'В комплекте нет поля {name}')
        values = bundle.get(name)
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            raise ValueError(f'Поле {name} должно быть списком объектов')
