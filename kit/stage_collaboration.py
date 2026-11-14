"""Вопросы команд и свидетельства проверок одной редакции этапа."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from uuid import UUID
import time

from .journal import TaskJournal
from .project_workspace import section_paths
from .task_control_state import plain_path


ROLES = frozenset(('manager', 'analyst', 'engineer'))
SECTIONS = frozenset(('requirements', 'bdd', 'nfr'))
THREAD_ACTIONS = ('ask', 'reply', 'reopen')


def _text(value, label, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{label}: требуется текст длиной от 1 до {limit} знаков.')
    return value.strip()


def _role(value):
    if not isinstance(value, str) or value not in ROLES:
        raise ValueError('Выберите роль РП, аналитика или инженерной команды.')
    return value


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except ValueError as error:
        raise ValueError('Для операции нужен UUID.') from error


def _positive(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f'{label}: нужен положительный номер.')
    return value


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _check_stage(vault, stage):
    if vault.record(stage)['kind'] != 'stage':
        raise ValueError('Выберите этап проекта.')


def _source_revision(db, stage, command):
    feature = command.get('feature', '')
    if not isinstance(feature, str):
        raise ValueError('Укажите ID фичи или оставьте поле пустым.')
    if feature:
        feature = _text(feature, 'ID фичи', 64)
        owner = db.execute("SELECT key,parent FROM records WHERE key=? AND kind='feature'", (feature,)).fetchone()
        if owner is None or owner['key'] != feature or owner['parent'] != stage:
            raise ValueError('Фича не принадлежит выбранному этапу.')
    revision = _positive(command.get('revision'), 'Редакция')
    row = None
    if _has_table(db, 'revision_history'):
        row = db.execute('SELECT document FROM revision_history WHERE record_key=? AND number=?',
                         (feature or stage, revision)).fetchone()
    if row is None:
        raise ValueError('Выбранная редакция ещё не записана в историю.')
    return feature, revision, json.loads(row['document'])


def _events(db, stage, thread=None, *, only_threads=False):
    if not _has_table(db, 'stage_collaboration'):
        return []
    sql = 'SELECT operation, document FROM stage_collaboration WHERE stage=?'
    params = [stage]
    if thread is not None:
        sql += ' AND object_id=?'
        params.append(thread)
    if only_threads:
        sql += " AND operation IN ('ask','reply','reopen')"
    return [(row[0], json.loads(row[1])) for row in db.execute(sql + ' ORDER BY rowid', params)]


def _project_events(events):
    threads, traces = {}, []
    for action, event in events:
        if action == 'trace':
            traces.append(event)
            continue
        if action == 'ask':
            threads[event['id']] = {key: event[key] for key in
                                    ('id', 'stage', 'feature', 'revision', 'subject', 'blocking')}
            threads[event['id']]['messages'] = []
            if event.get('risk_id'):
                threads[event['id']].update(risk_id=event['risk_id'], risk_hash=event.get('risk_hash'),
                                            has_risk_snapshot=event.get('has_risk_snapshot', False))
        thread = threads[event['id']]
        thread['status'] = 'answered' if action == 'reply' else 'open'
        thread['version'] = event['version']
        thread['assignee'] = event['assignee']
        thread['messages'].append({key: event[key] for key in ('action', 'role', 'author', 'assignee', 'text', 'at')})
    return {'threads': list(threads.values()), 'traces': traces}


def read_collaboration(vault, stage):
    """Чтение прежней базы допускается до первого действия пользователя."""
    _check_stage(vault, stage)
    with vault.reading() as db:
        return _project_events(_events(db, stage))


def blocking_threads(db, stage):
    """Вызывается в транзакции принятия решения, чтобы не пропустить новый вопрос."""
    threads = _project_events(_events(db, stage, only_threads=True))['threads']
    return [thread for thread in threads if thread['blocking'] and thread['status'] == 'open']


def _risk_context(vault, db, stage, feature, revision, snapshot, command):
    from .stage_risks import risk_in_note
    address = _text(command['risk_id'], 'ID риска', 220)
    key = feature or stage
    prefix = key + '/'
    if not address.startswith(prefix) or not address[len(prefix):]:
        raise ValueError('Выбранный риск относится к другой карточке.')
    latest = db.execute('SELECT max(number) FROM revision_history WHERE record_key=?', (key,)).fetchone()[0]
    source = command.get('risk_source', 'working' if revision == latest else 'archive')
    if source not in ('working', 'archive') or source == 'working' and revision != latest:
        raise ValueError('Редакция риска изменилась. Откройте паспорт повторно.')
    if source == 'working':
        note = section_paths(vault, key)['risks'].read_bytes()
        digest, text = hashlib.sha256(note).hexdigest(), note.decode('utf-8-sig')
    else:
        name = f'Документы/{key}/Риски.md'
        saved = snapshot.get('documents', {}).get(name)
        if not saved:
            raise ValueError('В выбранной редакции отсутствует документ рисков.')
        digest, text = saved['sha256'], saved['text']
    if command.get('risk_hash', digest) != digest:
        raise ValueError('Документ риска изменился. Откройте паспорт повторно и сверьте вопрос.')
    if not risk_in_note(text, address[len(prefix):]):
        raise ValueError('В выбранном документе риск с таким ID отсутствует.')
    db.execute('CREATE TABLE IF NOT EXISTS discussion_risk_sources (hash TEXT PRIMARY KEY, text TEXT NOT NULL)')
    db.execute('INSERT OR IGNORE INTO discussion_risk_sources VALUES (?,?)', (digest, text))
    return dict(risk_id=address, risk_hash=digest, has_risk_snapshot=True)


def discussion_risk_source(vault, stage, thread):
    """Возвращает тот документ риска, который команда обсуждала при постановке вопроса."""
    with vault.reading() as db:
        events = _events(db, stage, _uuid(thread), only_threads=True)
        if events and events[0][1].get('has_risk_snapshot'):
            digest = events[0][1]['risk_hash']
            row = db.execute('SELECT text FROM discussion_risk_sources WHERE hash=?', (digest,)).fetchone()
            if row:
                return row[0]
    raise FileNotFoundError('У этого обсуждения нет сохранённого документа риска.')


def _thread_event(vault, db, stage, action, identifier, command):
    assignee = _role(command.get('assignee'))
    text = _text(command.get('text'), 'Сообщение', 4000)
    if action == 'ask':
        feature, revision, snapshot = _source_revision(db, stage, command)
        context = _risk_context(vault, db, stage, feature, revision, snapshot, command) if command.get('risk_id') else {}
        blocking = command.get('blocking', False)
        if type(blocking) is not bool:
            raise ValueError('Признак блокирующего вопроса должен быть логическим.')
        subject = _text(command['subject'], 'Тема вопроса', 160) if command.get('subject') else ''
        return dict(id=identifier, stage=stage, feature=feature, revision=revision, version=1,
                    assignee=assignee, text=text, subject=subject, blocking=blocking, **context)

    thread_id = _uuid(command.get('thread_id'))
    history = _events(db, stage, thread_id, only_threads=True)
    if not history:
        raise ValueError('В выбранном этапе нет такого обсуждения.')
    last_action, previous = history[-1]
    if _positive(command.get('expected_version'), 'Версия обсуждения') != previous['version']:
        raise ValueError('Обсуждение изменилось. Прочтите новый ответ и повторите действие.')
    if (action == 'reopen') != (last_action == 'reply'):
        raise ValueError('Для ответа нужен открытый вопрос, для возобновления — вопрос с ответом.')
    return dict(id=thread_id, stage=stage, version=previous['version'] + 1, assignee=assignee, text=text)


def _case_result(log: Path, case_name: str):
    """Имя и исход конкретного теста подтверждаются одной строкой подробного отчёта unittest."""
    pattern = re.compile(r"^([\w]+) \(([\w.]+)\) \.\.\. (ok|FAIL|ERROR|skipped(?: .*)?|expected failure|unexpected success)$")
    outcomes = []
    with log.open(encoding='utf-8-sig', errors='replace') as source:
        for line in source:
            match = pattern.fullmatch(line.strip())
            if match:
                method, name, result = match.groups()
                if not name.endswith('.' + method):
                    name += '.' + method
                if name == case_name:
                    outcomes.append(result)
    if len(outcomes) != 1:
        raise ValueError('Для этого имени в журнале нужна одна строка с исходом теста. Можно связать весь прогон.')
    status = outcomes[0]
    if status.startswith('skipped'):
        return 'skipped'
    return {'ok': 'passed', 'FAIL': 'failed', 'ERROR': 'error', 'expected failure': 'expected_failure',
            'unexpected success': 'unexpected_success'}[status]


def _test_evidence(vault, db, stage, feature, command):
    task = _text(command.get('task_id'), 'ID задачи', 80)
    journal = TaskJournal(vault.root)
    folder = journal.task_dir(task)
    plain_path(folder / 'events.jsonl')
    events = journal.read(task)
    if _has_table(db, 'revision_requests'):
        owner = db.execute('SELECT record_key FROM revision_requests WHERE id=?', (task,)).fetchone()
        if owner and owner[0] != (feature or stage):
            raise ValueError('Заявка исполнителя принадлежит другой записи проекта.')
        if re.fullmatch(r'doc-[0-9a-f]{32}', task) and not owner:
            raise ValueError('В проектной базе нет заявки с таким ID.')
    existing = db.execute("SELECT stage FROM stage_collaboration WHERE task_id=? AND stage<>? LIMIT 1",
                          (task, stage)).fetchone()
    if existing:
        raise ValueError('Задача зарегистрирована в другом этапе.')
    test_id = command.get('test_event_id') or ''
    case = command.get('case_name') or ''
    if not test_id:
        if case:
            raise ValueError('Для отдельного теста укажите событие запуска.')
        return task, '', None
    selected = [event for event in events if event['kind'] == 'test' and event.get('event_id') == test_id]
    if len(selected) != 1:
        raise ValueError('В журнале выбранной задачи нет однозначного события этого прогона.')
    event = selected[0]
    test = {key: event.get(key) for key in ('scope', 'status', 'tests', 'failures', 'errors', 'skipped', 'at')}
    log_name = event.get('log') or ''
    log = None
    if isinstance(log_name, str) and re.fullmatch(r'test-[0-9a-f]{32}\.log', log_name):
        candidate = plain_path(folder / log_name)
        if candidate.is_file():
            log = candidate
    test['log'] = log.name if log else ''
    if case:
        case = _text(case, 'Полное имя теста', 350)
        if not log:
            raise ValueError('Файл результата недоступен для проверки конкретного теста.')
        test.update(case_name=case, case_status=_case_result(log, case))
    return task, test_id, test


def _trace_event(vault, db, stage, identifier, command):
    feature, revision, snapshot = _source_revision(db, stage, command)
    key = feature or stage
    section = command.get('section')
    if not isinstance(section, str) or section not in SECTIONS:
        raise ValueError('Правило должно относиться к требованиям, BDD или NFR.')
    source = command.get('source', 'archive')
    path = section_paths(vault, key)[section]
    relative = path.relative_to(vault.path).as_posix()
    if source == 'archive':
        archived = snapshot.get('documents', {}).get(relative)
        if not archived:
            raise ValueError('Для этого документа в выбранной редакции нет снимка.')
        note, digest = archived['text'], archived['sha256']
        state = 'accepted' if snapshot.get('status') == 'accepted' else 'initial'
    elif source == 'working':
        latest = db.execute('SELECT MAX(number) FROM revision_history WHERE record_key=?', (key,)).fetchone()[0]
        if latest != revision:
            raise ValueError('Базовая редакция рабочего документа изменилась. Обновите монитор.')
        content = path.read_bytes()
        note, digest = content.decode('utf-8-sig'), hashlib.sha256(content).hexdigest()
        state = 'working'
    else:
        raise ValueError('Выберите рабочий документ или архивную редакцию.')
    rule = _text(command.get('rule'), 'Текст инварианта', 4000)
    if rule not in note:
        raise ValueError('Цитата отсутствует в выбранном документе этой редакции.')
    invariant = _text(command.get('invariant'), 'ID инварианта', 100)
    task, test_id, test = _test_evidence(vault, db, stage, feature, command)
    return dict(id=identifier, stage=stage, feature=feature, revision=revision, invariant=invariant,
                section=section, source=source, source_state=state, rule=rule, source_hash=digest,
                working_path=relative, archive_path=f'История/{key}/Редакция {revision:04}.md',
                task_id=task, test_event_id=test_id, test=test)


def apply_action(vault, stage, command):
    """Сохраняет одно действие. Повтор того же запроса возвращает исходный результат."""
    _check_stage(vault, stage)
    if not isinstance(command, dict) or command.get('project_id') != vault.project_id:
        raise ValueError('Команда относится к другому проекту.')
    identifier = _uuid(command.get('operation_id'))
    action = command.get('action')
    if action not in (*THREAD_ACTIONS, 'trace'):
        raise ValueError('Неизвестное действие командного монитора.')
    role = _role(command.get('role'))
    author = _text(command['author'], 'Имя автора', 120) if command.get('author') else ''
    request = json.dumps(command, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    fingerprint = hashlib.sha256(request.encode('utf-8')).hexdigest()
    with vault.locked() as db:
        # События неизменяемы. object_id объединяет переписку, operation_id различает отправки.
        db.execute('''CREATE TABLE IF NOT EXISTS stage_collaboration (
            operation_id TEXT PRIMARY KEY, stage TEXT NOT NULL, object_id TEXT NOT NULL,
            operation TEXT NOT NULL, task_id TEXT, fingerprint TEXT NOT NULL, document TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS stage_collaboration_context ON stage_collaboration(stage,object_id)')
        db.execute('CREATE INDEX IF NOT EXISTS stage_collaboration_tasks ON stage_collaboration(task_id)')
        previous = db.execute('SELECT stage,fingerprint,document FROM stage_collaboration WHERE operation_id=?',
                              (identifier,)).fetchone()
        if previous:
            if previous['stage'] != stage or previous['fingerprint'] != fingerprint:
                raise ValueError('Под этим ID уже сохранена другая операция.')
            return json.loads(previous['document'])
        if action == 'trace':
            event = _trace_event(vault, db, stage, identifier, command)
        else:
            event = _thread_event(vault, db, stage, action, identifier, command)
        event.update(action=action, role=role, author=author, at=time.time())
        db.execute('INSERT INTO stage_collaboration VALUES (?,?,?,?,?,?,?)',
                   (identifier, stage, event['id'], action, event.get('task_id'), fingerprint,
                    json.dumps(event, ensure_ascii=False)))
        return event
