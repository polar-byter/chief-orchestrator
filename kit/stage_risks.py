"""Сводный реестр этапа. Паспорт описывает риск, а в смету попадает решение о резерве."""
from __future__ import annotations

from fractions import Fraction
from hashlib import sha256
import re

from .effort_estimate import display_hours, hours_value
from .estimate_documents import RISK_COLUMNS, _ids, _table_lines
from .role_tasks import document_lines
from .stage_decisions import SHARED_RISK_COLUMNS
from .task_control_state import plain_path


PASSPORT_COLUMNS = ('Поле', 'Значение')
PASSPORT_HEADING = re.compile(r'^###\s+([^\s/]+)\s+[·—]\s+(.+?)\s*$')
# Левая колонка сохраняет имена из существующих заметок и их сокращённый вариант.
FIELDS = {
    'cause': ('Причина', 'Причина → возможное событие → последствие'),
    'event': ('Событие',),
    'impact': ('Последствие', 'Влияние', 'Влияние: чел·ч, дни, стоимость или нарушаемый NFR'),
    'probability': ('Вероятность', 'Вероятность / категория и основание'),
    'basis': ('Основание', 'Источник, выборка, период и условия'),
    'owner': ('Владелец', 'Владелец и исполнитель меры'),
    'response': ('Ответ', 'Ответ: принять, снизить, избежать или передать'),
    'mitigation': ('Мера', 'Мера, срок проверки и сигнал срабатывания'),
    'review_date': ('Дата пересмотра', 'Следующий пересмотр и решение РП'),
    'outcome': ('Исход', 'Инцидент и фактические затраты'),
    'residual': ('Остаточный риск', 'Наблюдение после меры и остаточный риск'),
    'trigger': ('Сигнал',),
    'contingency': ('Действие при наступлении',),
    'horizon': ('Горизонт оценки',),
    'common_cause': ('Общая причина с другими рисками / работами',),
    'work_ids': ('ID работ',),
}
FIELDS_BY_LABEL = {label.casefold(): field for field, labels in FIELDS.items() for label in labels}
EMPTY_VALUES = frozenset(('—', '', 'нет данных', 'неизвестна', 'неизвестно', 'не указаны', 'не описаны',
                          'не определён', 'не определена', 'не определены', 'не определено',
                          'не проверена', 'не назначены', 'решение рп не принято'))


def _sections(note):
    heading, lines = '', []
    for line in document_lines(note):
        if re.match(r'^\s{0,3}#{1,3}\s', line):
            yield heading, '\n'.join(lines)
            heading, lines = line.strip(), []
        else:
            lines.append(line)
    yield heading, '\n'.join(lines)


def _passport(body):
    fields = {}
    for label, value in _table_lines(body, PASSPORT_COLUMNS) or []:
        field = FIELDS_BY_LABEL.get(label.casefold())
        if not field or value.casefold() in EMPTY_VALUES:
            continue
        if field in fields:
            raise ValueError(f'Поле «{label}» указано дважды.')
        fields[field] = value
    return fields


def _entries(note):
    passports, allocations, issues = {}, {}, []
    for heading, body in _sections(note):
        match = PASSPORT_HEADING.fullmatch(heading)
        if not match:
            continue
        risk_id, title = match.groups()
        try:
            fields = _passport(body)
            # У шаблона есть значение по умолчанию про инцидент. Оно не создаёт реальный риск.
            if (title == 'Название риска' and not any(name != 'outcome' or text != 'Событие не зарегистрировано'
                                                        for name, text in fields.items())):
                continue
            if risk_id in passports:
                raise ValueError('Два паспорта одного риска. Уточните ID в заголовках.')
            passports[risk_id] = dict(fields, title=title)
        except ValueError as error:
            issues.append(f'{risk_id} — {error}')
            passports[risk_id] = {}
    try:
        rows = _table_lines(note, RISK_COLUMNS) or []
        for risk_id, works, mode, hours in rows:
            if not risk_id:
                issues.append('В распределении резерва есть строка без ID риска.')
                continue
            if risk_id in allocations:
                issues.append(f'{risk_id} — повторная строка распределения резерва.')
                allocations[risk_id] = {}
                continue
            allocations[risk_id] = dict(works=list(_ids(works)), treatment=mode or None, hours=hours)
    except ValueError as error:
        issues.append(str(error))
    return passports, allocations, issues


def risk_in_note(note: str, risk_id: str) -> bool:
    """Ищет точный локальный ID для привязки вопроса к рабочей заметке или её снимку."""
    passports, allocations, _ = _entries(note)
    return bool(passports.get(risk_id) or allocations.get(risk_id))


def _group_names(note, duplicates):
    result = {}
    for event, addresses, owner in _table_lines(note, SHARED_RISK_COLUMNS) or []:
        members = [x.strip() for x in addresses.split(',')]
        if owner in members and len(members) > 1 and all(key == owner or duplicates.get(key) == owner for key in members):
            for key in members:
                result[key] = (event, members)
    return result


def _hours(number):
    return float(display_hours(number).replace(',', '.')) if number is not None else None


def risk_register(vault, stage: str, forecast: dict) -> dict:
    """Читает документы этапа. Группировка и суммы принадлежат той же оценке, что и сводка РП."""
    feature_rows = {item['key']: item for item in forecast['features']}
    records = vault.records()
    if not any(record['key'] == stage and record['kind'] == 'stage' for record in records):
        raise ValueError('Выберите существующий этап проекта.')
    duplicates = forecast.get('shared_reserves', {})
    verified = forecast.get('risk_allowances', {})
    expected_hashes = forecast.get('risk_source_hashes', {})
    issues = list(forecast.get('issues', ()))
    totals = {key: forecast.get(key) for key in ('base_hours', 'reserve_hours', 'known_hours', 'total_hours')}
    documents, groups = [], {}
    changed = set()
    for record in records:
        key = record['key']
        if key != stage and record['parent'] != stage:
            continue
        try:
            raw = plain_path(vault.path / 'Документы' / key / 'Риски.md').read_bytes()
            note = raw.decode('utf-8-sig')
        except (OSError, ValueError) as error:
            changed.add(key)
            issues.append(f'{key} — не удалось прочитать раздел рисков: {error}')
            continue
        digest = sha256(raw).hexdigest()
        consistent = key not in expected_hashes or digest == expected_hashes[key]
        if not consistent:
            changed.add(key)
            issues.append(f'{key} — документ изменён после расчёта. Обновите сводку.')
        if key == stage and consistent:
            try:
                groups = _group_names(note, duplicates)
            except ValueError as error:
                issues.append(f'{key} — {error}')
        passports, allocations, problems = _entries(note)
        issues.extend(f'{key} — {problem}' for problem in problems)
        revision = forecast.get('stage_revision') if key == stage else feature_rows.get(key, {}).get('revision')
        documents.append((key, record['title'], revision, digest, passports, allocations))

    rows = []
    for key, record_title, revision, digest, passports, allocations in documents:
        for local_id in dict.fromkeys((*passports, *allocations)):
            address = f'{key}/{local_id}'
            passport = passports.get(local_id, {})
            fields = {name: passport.get(name) for name in FIELDS if name != 'work_ids'}
            allocation = allocations.get(local_id, {})
            work_ids = allocation.get('works') or list(_ids(passport.get('work_ids', '')))
            mode = allocation.get('treatment')
            group, members = groups.get(address, (None, []))
            source_keys = {key, stage, *(member.split('/', 1)[0] for member in members)}
            try:
                declared = _hours(hours_value(allocation.get('hours', ''), local_id))
            except ValueError as error:
                issues.append(f'{key} — {error}')
                declared = None
            credited, owner = None, None
            # В смету входят только решения, прошедшие проверки работ, O/M/P и общей причины.
            if changed.isdisjoint(source_keys) and verified.get(address) is not None and totals['reserve_hours'] is not None:
                owner = duplicates.get(address, address)
                if owner == address:
                    credited = _hours(Fraction(verified[address]))
                else:
                    credited = 0.0
            rows.append(dict(id=address, key=key, risk_id=local_id, record_title=record_title,
                             title=passport.get('title') or local_id, works=work_ids, treatment=mode,
                             declared_hours=declared, added_hours=credited, counted_at=owner,
                             group=group, group_members=members, revision=revision, source_hash=digest, **fields))
    if changed:
        # Общая сумма могла устареть даже при неизменном документе одной из фич.
        totals['reserve_hours'] = totals['known_hours'] = totals['total_hours'] = None
    return dict(rows=rows, issues=list(dict.fromkeys(issues)), **totals)
