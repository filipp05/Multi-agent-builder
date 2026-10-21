from app.filipp.brief import LABELS, format_field_value

STATUS_LABELS={'queued':'Ожидает обработки','running':'Уточняем требования','waiting_input':'Нужны уточнения',
 'ready_to_confirm':'Проверьте сводку','confirmed':'Требования подтверждены','failed':'Не удалось получить ответ',
 'cancelled':'Расчёт остановлен','interrupted':'Расчёт был прерван','blocked_scope':'Есть неподдерживаемые работы'}

EXTRA_LABELS={'start_date':'Дата начала','deadline':'Крайний срок (включительно)', 'room.surface_condition':'Состояние основания','currency':'Валюта','hard_preferences':'Обязательные пожелания',
 'soft_preferences':'Желательные пожелания','excluded_work':'Исключённые работы','calendar.timezone':'Часовой пояс',
 'calendar.work_weekdays':'Рабочие дни','calendar.day_start_hour':'Начало рабочего дня',
 'calendar.hours_per_day':'Часов в день','calendar.crew_count':'Бригад'}

def present(s):
    result=dict(s)
    result['status_label']=STATUS_LABELS[s['status']]
    result['warnings']=['Электрику пока не можем реализовать. Она исключена из обрабатываемых работ; остальные требования сохраняются.'] if s['draft'].get('ignored_requests') else []
    result['review']=[{'field':key,'label':{**LABELS,**EXTRA_LABELS}.get(key,key),'value':value,
                       'proposed':s['draft']['origins'].get(key)=='proposal'} for key,value in s['draft']['values'].items()]
    result['scope_notice']='Поддерживаемый объём: покраска стен и ламинат в одной прямоугольной сухой комнате.'
    result['next_action']={
        'blocked_scope':'Этот запрос нельзя подтвердить. Можно начать новый проект в поддерживаемых границах. Его поля нужно заполнить заново; данные автоматически не переносятся.',
        'waiting_input':'Ответьте на вопросы, чтобы уточнить требования.',
        'ready_to_confirm':'Проверьте сводку, включая предложенный календарь, и подтвердите требования.',
        'confirmed':'Требования сохранены. Полный план ремонта — следующий этап.',
        'failed':'Ввод сохранён, обработка не завершена. Нажмите «Повторить», чтобы обработать сохранённый ввод.',
        'cancelled':'Можно повторить расчёт или изменить требования.',
        'interrupted':'Можно повторить прерванный расчёт.'}.get(s['status'],'Дождитесь окончания обработки или остановите расчёт.')
    if s['status']=='failed':
        code=(s.get('error') or {}).get('code')
        if code in ('auth','quota','cli_missing'):
            action={'auth':'Войдите в Codex через ChatGPT, затем нажмите «Повторить».',
                    'quota':'Дождитесь восстановления лимита подписки и повторите позже.',
                    'cli_missing':'Установите Codex CLI и войдите через ChatGPT перед повтором.'}[code]
            result['next_action']='Ввод сохранён, обработка не завершена. '+action
    if s['draft']['unsupported_requests']:
        result['warnings'].append('Пока не поддерживаем: '+ '; '.join(s['draft']['unsupported_requests'])+'.')
    for row in result['review']:
        key=row['field'];conflict=s['draft']['conflicts'].get(key)
        row['conflicted']=bool(conflict)
        row['display_value']=format_field_value(key,row['value'])
        if conflict:
            row['display_value']='Нужно уточнить: '+format_field_value(key,conflict['previous'])+' или '+format_field_value(key,conflict['candidate'])
        elif row['proposed'] and key in ('calendar.work_weekdays','calendar.day_start_hour','calendar.hours_per_day'):
            row['display_value']+=' — принято' if s['status']=='confirmed' else ' — предложение'
    return result
