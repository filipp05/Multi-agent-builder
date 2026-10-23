"""Conservative scope gate for the fixed paint-and-laminate MVP.

Only explicit, known out-of-scope exclusions are accepted. Arbitrary natural
language is not classified by keywords or silently delegated to the LLM.
"""
ALLOWED_EXCLUSIONS = frozenset({'потолок', 'мебель', 'электрика', 'сантехника', 'перепланировка'})
SCOPE_MESSAGE = ('В текущем MVP вместе рассчитываются подготовка, покраска стен и укладка ламината. '
                 'Указанные исключения нельзя проверить в этом сценарии. Уточните объём работ; '
                 'не убирайте важные для вас ограничения ради запуска расчёта.')


def unsupported_exclusions(brief):
    return [value for value in brief.get('excluded_work', [])
            if value.strip().casefold() not in ALLOWED_EXCLUSIONS]
