"""Заявки на доработку и решения о приёмке для проектной базы Чифа."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import unified_diff
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .journal import TaskJournal
from .project_vault import publish_new
from .project_workspace import card_navigation, document_snapshot, ensure_workspace, section_paths
from .estimate_documents import calculation_for_snapshot
from .workspace_estimates import input_addition
from .task_control_state import plain_path


DRAFT_HEADING = '## Что изменить'
SUBMIT_MARK = '- [ ] Передать Чифу'


def workspace_template(key: str, title: str, card_path: Path | None = None) -> str:
    launcher = r'.\kit\python.cmd' if os.name == 'nt' else 'sh kit/python.sh'
    backlink = f'[[{card_path.with_suffix("").as_posix()}|Открыть карточку]] · ' if card_path else ''
    return (f'# Доработка · {title}\n\n'
            f'Запись `{key}`. Под заголовком «Что изменить» опишите изменение и признак готовности. '
            'Затем отметьте флажок. Для следующего запроса или повтора после ошибки снимите '
            'отметку и сохраните заметку. Через две секунды можно снова отметить её, когда текст будет готов.\n\n'
            f'{SUBMIT_MARK}\n\n'
            f'Команда для терминала проекта — `{launcher} -m kit.project_docs submit --key {key}`.\n\n'
            f'## Квитанции\n\n```query\npath:"Квитанции/{key}/"\n```\n\n'
            f'{backlink}[[История/{key}/Редакции|История редакций]]\n\n{DRAFT_HEADING}\n\n')


_SUBMISSION_CHECKBOX = re.compile(r'^- \[([ xX])\] Передать Чифу[ \t]*$', re.MULTILINE)


def _workspace_parts(note: str) -> tuple[str, str]:
    lines = note.splitlines()
    if DRAFT_HEADING not in lines:
        raise ValueError(f'В рабочей заметке отсутствует заголовок «{DRAFT_HEADING}».')
    heading_at = lines.index(DRAFT_HEADING)
    return '\n'.join(lines[:heading_at]), '\n'.join(lines[heading_at + 1:]).strip()


def marked_for_submission(note: str) -> bool:
    header, _ = _workspace_parts(note)
    return any(mark.lower() == 'x' for mark in _SUBMISSION_CHECKBOX.findall(header))


def _workspace_digest(content: bytes) -> str:
    # Флажок доставляет заявку, а пользовательские условия находятся в тексте.
    # Снятие отметки после отправки не меняет предмет приёмки.
    header, text = _workspace_parts(content.decode('utf-8-sig'))
    normalized = _SUBMISSION_CHECKBOX.sub(SUBMIT_MARK, header) + '\n' + DRAFT_HEADING + '\n' + text
    return _digest(normalized.encode('utf-8'))


@contextmanager
def _revision_store(vault):
    # Таблицы создаются и при подключении базы, созданной более ранним комплектом.
    with vault.locked() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS revision_history (
            record_key TEXT NOT NULL, number INTEGER NOT NULL, document TEXT NOT NULL,
            PRIMARY KEY(record_key, number))''')
        db.execute('''CREATE TABLE IF NOT EXISTS revision_requests (
            id TEXT PRIMARY KEY, record_key TEXT NOT NULL, base INTEGER NOT NULL,
            status TEXT NOT NULL, document TEXT NOT NULL)''')
        yield db


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _history(vault, key: str) -> Path:
    return plain_path(vault.path / 'История' / key)


def _note_path(key: str, number: int) -> str:
    return f'История/{key}/Редакция {number:04}.md'


def _archive(path: Path, text: str) -> None:
    data = text.encode('utf-8')
    if not publish_new(path, data) and path.read_bytes() != data:
        raise ValueError(f'Архивная заметка изменена вручную: {path}. Сохраните свою версию отдельно и повторите действие.')


def _quote_note(note: str) -> str:
    # В снимке может быть встраивание самого индекса истории. Показываем исходный
    # Markdown буквально, чтобы старая редакция не менялась вслед за новой.
    longest = max((len(match[0]) for match in re.finditer(r'`+', note)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}markdown\n{note.rstrip()}\n{fence}\n'


def _render_revision(data: dict) -> str:
    key, number = data['key'], data['revision']
    heading = f'# {data["title"]} · редакция {number}\n\n'
    if number == 1:
        return (heading + 'Исходное описание при добавлении записи.\n\n'
                + _quote_note(data['card_text']) + _render_documents(data))
    previous = Path(_note_path(key, number - 1)).with_suffix('').as_posix()
    text = (heading + f'[[{previous}|Предыдущая редакция]] · заявка `{data["id"]}`\n\n'
            f'## Запрошенное изменение\n\n{data["text"]}\n\n'
            f'## Принятый результат\n\n{data["result"]}\n\n'
            f'## Проверка\n\n{data["evidence"]}\n\n')
    if data['task']:
        text += (f'Задача `{data["task"]}`. [Журнал задания]({data["journal_url"]}) · '
                 f'[Результат полного теста]({data["log_url"]}). '
                 f'Событие теста `{data["test_event"]}`.\n\n')
    else:
        text += 'Основание приёмки записано оператором.\n\n'
    if data.get('kind') == 'stage':
        for label, field in (('При постановке', 'context'), ('При приёмке', 'accepted_context')):
            features = data.get(field, [])
            text += f'## {label} · фичи этапа\n\n'
            for feature in features:
                path = Path(_note_path(feature['key'], feature['revision'])).with_suffix('').as_posix()
                text += f'- [[{path}|{feature["key"]} · редакция {feature["revision"]}]] — {feature["title"]}\n'
            text += '\n' if features else 'Фичи ещё не заведены.\n\n'
    return (text + '## Снимок карточки при постановке\n\n' + _quote_note(data['card_text'])
            + _render_documents(data))


def _render_documents(data: dict) -> str:
    text = '\n## Трудозатраты и резерв при постановке\n\n' + data['estimate'] if 'estimate' in data else ''
    for name, snapshot in data.get('documents', {}).items():
        text += f'\n## Документ при постановке · {name}\n\n' + _quote_note(snapshot['text'])
    return text


def _render_index(key: str) -> str:
    return (f'# История {key}\n\n[[История/{key}/Редакция 0001|Исходная редакция]]\n\n'
            '## Последнее решение\n\n'
            f'```base\nfilters:\n  and:\n    - file.inFolder("История/{key}")\n'
            '    - file.name.startsWith("Редакция ")\nviews:\n  - type: table\n'
            '    name: Редакции\n    order:\n      - file.name\n'
            '    sort:\n      - property: file.name\n        direction: DESC\n```\n')


def ensure_history(vault, key: str) -> Path:
    with _revision_store(vault) as db:
        record = vault.record(key)
        latest = _latest(db, key)
        if latest is None:
            documents = document_snapshot(vault, key)
            latest = dict(key=key, title=record['title'], project_id=vault.project_id, revision=1,
                          card_text=vault.card(key).read_text(encoding='utf-8-sig'),
                          documents=documents, estimate=calculation_for_snapshot(key, documents))
            db.execute('INSERT INTO revision_history VALUES (?, 1, ?)', (key, _json(latest)))
        # После сбоя во время записи транзакция откатится; полностью записанные файлы
        # допустимы при повторе. Никогда не заменяем архив или пользовательский индекс.
        path = _history(vault, key) / 'Редакции.md'
        publish_new(path, _render_index(key).encode('utf-8'))
        _archive(plain_path(vault.path / _note_path(key, latest['revision'])), _render_revision(latest))
        return path


def _latest(db, key: str) -> dict | None:
    row = db.execute('SELECT document FROM revision_history WHERE record_key=? '
                     'ORDER BY number DESC LIMIT 1', (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _read_request(row) -> dict:
    return dict(json.loads(row['document']), status=row['status'])


def overview_revisions(db) -> tuple[dict, dict]:
    """Снимки последней редакции и открытых заявок для чтения общего обзора."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    latest, pending = {}, {}
    if 'revision_history' in tables:
        rows = db.execute('SELECT h.record_key, h.document FROM revision_history h JOIN '
                          '(SELECT record_key, max(number) AS number FROM revision_history GROUP BY record_key) last '
                          'ON last.record_key=h.record_key AND last.number=h.number')
        latest = {row[0]: json.loads(row[1]) for row in rows}
    if 'revision_requests' in tables:
        rows = db.execute("SELECT * FROM revision_requests WHERE status='pending' ORDER BY rowid")
        pending = {row['record_key']: _read_request(row) for row in rows}
    return latest, pending


def _comparable_card(request: dict, content: bytes) -> bytes:
    # Обновление кита добавляет ссылки к старой карточке. Убираем только точный
    # служебный хвост; любые правки исходного текста по-прежнему видны в compare.
    if 'documents' not in request and _digest(content) != request['card_sha256']:
        return content.removesuffix(card_navigation(request['key']).encode('utf-8'))
    return content


def _related_records(vault, db, record: dict) -> list[dict]:
    # Obsidian строит список фич при открытии этапа. Сам Markdown этапа не меняется
    # при добавлении фичи, поэтому храним состав этапа на момент решения отдельно.
    if record['kind'] == 'feature':
        related = [vault.record(record['parent'])]
    else:
        related = [item for item in vault.records() if item['parent'] == record['key']]
    context = []
    for item in related:
        latest = _latest(db, item['key'])
        context.append(dict(key=item['key'], title=item['title'], revision=latest['revision'] if latest else 1))
    return context


def request_revision(vault, key: str, *, expected_workspace: bytes | None = None) -> dict:
    ensure_workspace(vault, key)
    ensure_history(vault, key)
    with _revision_store(vault) as db:
        record = vault.record(key)
        card = vault.card(key).read_bytes()
        workspace = vault.workspace(key).read_bytes()
        if expected_workspace is not None and workspace != expected_workspace:
            raise ValueError('Рабочая заметка изменилась после отметки. Сверьте текст и снова отметьте флажок.')
        _, text = _workspace_parts(workspace.decode('utf-8-sig'))
        if not text:
            raise ValueError('Опишите изменение под заголовком «Что изменить».')
        card_hash, draft_hash = _digest(card), _workspace_digest(workspace)
        documents = document_snapshot(vault, key)
        row = db.execute('SELECT * FROM revision_requests WHERE record_key=? '
                         'ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
        if row:
            previous = _read_request(row)
            comparable = (document_snapshot(vault, key, legacy_documents=previous['documents'])
                          if 'documents' in previous and 'estimate' not in previous else documents)
            if ((previous['card_sha256'], previous['workspace_sha256']) == (card_hash, draft_hash)
                    and previous.get('documents') == comparable):
                if previous['status'] == 'accepted':
                    return json.loads(db.execute('SELECT document FROM revision_history '
                                                 'WHERE record_key=? AND number=?',
                                                 (key, previous['base_revision'] + 1)).fetchone()[0])
                return previous
        base = _latest(db, key)
        identifier = 'doc-' + uuid.uuid4().hex
        data = dict(id=identifier, task_id=identifier, project_id=vault.project_id, key=key,
                    kind=record['kind'], stage=record['parent'] or key, title=record['title'],
                    base_revision=base['revision'], base_note=_note_path(key, base['revision']),
                    base_result=base.get('result', ''), card_sha256=card_hash, workspace_sha256=draft_hash,
                    card_text=card.decode('utf-8-sig'), text=text, requested_at=_now(),
                    context=_related_records(vault, db, record), documents=documents,
                    estimate=calculation_for_snapshot(key, documents))
        # Повторная подача изменённого черновика закрывает прежнюю заявку на ту же
        # запись. Обе версии сохраняются для разбора расхождений.
        db.execute("UPDATE revision_requests SET status='superseded' WHERE record_key=? AND status='pending'", (key,))
        db.execute("INSERT INTO revision_requests VALUES (?, ?, ?, 'pending', ?)",
                   (identifier, key, data['base_revision'], _json(data)))
        return dict(data, status='pending')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def pending_requests(vault) -> list[dict]:
    with _revision_store(vault) as db:
        rows = db.execute("SELECT * FROM revision_requests WHERE status='pending' ORDER BY rowid")
        return [_read_request(row) for row in rows]


def compare_revision(vault, request_id: str) -> str:
    """Сравнивает сохранённую заявку с текущими заметками, в том числе после повторной подачи."""
    with _revision_store(vault) as db:
        row = db.execute('SELECT * FROM revision_requests WHERE id=?', (request_id,)).fetchone()
        if row is None:
            raise ValueError('В этом проекте нет такой заявки.')
        request = _read_request(row)
        card = vault.card(request['key'])
        work = vault.workspace(request['key'])
        card_bytes = _comparable_card(request, card.read_bytes())
        work_bytes = work.read_bytes()
    work_note = work_bytes.decode('utf-8-sig')
    differences = []
    try:
        _, text = _workspace_parts(work_note)
    except ValueError:
        text = work_note
        differences.append('В рабочей заметке удалён заголовок «Что изменить». Ниже показан её полный текст.')
    for path, submitted, current in ((card, request['card_text'], card_bytes.decode('utf-8-sig')),
                                     (work, request['text'], text)):
        name = path.relative_to(vault.path).as_posix()
        lines = unified_diff(submitted.splitlines(), current.splitlines(),
                             fromfile=f'{name} (заявка {request_id})', tofile=f'{name} (сейчас)', lineterm='')
        differences.extend(lines)
    if 'documents' in request:
        for kind, path in section_paths(vault, request['key']).items():
            name = path.relative_to(vault.path).as_posix()
            snapshot = request['documents'][name]
            if not path.is_file():
                differences.append(f'Документ удалён: {name}')
                continue
            content = path.read_bytes()
            if 'estimate' not in request and kind in ('effort', 'risks'):
                content = content.removesuffix(input_addition(snapshot['text'], request['key'], kind).encode('utf-8'))
            lines = list(unified_diff(snapshot['text'].splitlines(), content.decode('utf-8-sig').splitlines(),
                                     fromfile=f'{name} (заявка {request_id})', tofile=f'{name} (сейчас)', lineterm=''))
            differences.extend(lines)
            if not lines and snapshot['sha256'] != _digest(content):
                differences.append(f'{name}: изменились кодировка или окончание строки.')
    if differences:
        return '\n'.join(differences)
    if request['card_sha256'] != _digest(card_bytes):
        return 'Текст карточки совпадает построчно, но изменились кодировка или окончание строки.'
    if request['workspace_sha256'] != _workspace_digest(work_bytes):
        return 'Карточка и текст изменения совпадают. Изменилась служебная часть рабочей заметки.'
    return ('Карточка, текст изменения и документы совпадают с заявкой.' if 'documents' in request
            else 'Карточка и текст изменения совпадают с заявкой. Снимки документов в старой заявке отсутствуют.')


def _checked_journal(vault, request: dict, task: str) -> dict:
    if task != request['task_id']:
        raise ValueError(f'Для этой заявки нужен журнал задачи {request["task_id"]}.')
    journal = TaskJournal(vault.root)
    folder = journal.task_dir(task)
    path = plain_path(folder / 'events.jsonl')
    events = journal.read(task)
    if events[-1]['kind'] != 'finish':
        raise ValueError('Завершите задачу Чифа до приёмки редакции.')
    runs = [event for event in events if event['kind'] == 'test' and event.get('scope') == 'full'
            and event.get('phase', 'work') == 'work' and not event.get('delegation_id')]
    if not runs or runs[-1].get('status') != 'passed' or runs[-1].get('returncode') != 0:
        raise ValueError('Последний полный тест этой задачи должен завершиться успешно.')
    run = runs[-1]
    if not re.fullmatch(r'test-[a-f0-9]{32}\.log', run.get('log') or ''):
        raise ValueError('У результата теста отсутствует ссылка на журнал прогона.')
    log = plain_path(folder / run['log'])
    if not log.is_file():
        raise ValueError('Файл журнала полного прогона не найден.')
    return dict(journal_url=path.as_uri(), log_url=log.as_uri(), test_event=run['event_id'])


def accept_revision(vault, key: str, request_id: str, result: str, evidence: str, *, task: str | None = None) -> dict:
    result, evidence = result.strip(), evidence.strip()
    if not result or not evidence:
        raise ValueError('Для приёмки нужны принятый результат и фактически выполненная проверка.')
    with _revision_store(vault) as db:
        record = vault.record(key)
        row = db.execute('SELECT * FROM revision_requests WHERE id=? AND record_key=?', (request_id, key)).fetchone()
        if row is None:
            raise ValueError('Заявка не принадлежит этой записи.')
        request = _read_request(row)
        number = request['base_revision'] + 1
        path = plain_path(vault.path / _note_path(key, number))
        if request['status'] == 'accepted':
            stored = db.execute('SELECT document FROM revision_history WHERE record_key=? AND number=?',
                                (key, number)).fetchone()
            accepted = json.loads(stored[0])
            if (result, evidence, task) != (accepted['result'], accepted['evidence'], accepted['task']):
                raise ValueError('По заявке уже записано другое решение. Откройте новую доработку.')
            _archive(path, _render_revision(accepted))
            return accepted
        if request['status'] != 'pending' or _latest(db, key)['revision'] != request['base_revision']:
            raise ValueError('Исходная редакция или заявка сменилась. Сверьте последнюю заявку и историю.')
        checked = _checked_journal(vault, request, task) if task else {}
        if (_digest(_comparable_card(request, vault.card(key).read_bytes())) != request['card_sha256'] or
                _workspace_digest(vault.workspace(key).read_bytes()) != request['workspace_sha256'] or
                ('documents' in request and document_snapshot(vault, key, legacy_documents=(
                    request['documents'] if 'estimate' not in request else None)) != request['documents'])):
            raise ValueError(f'После подачи заявки {request_id} изменились карточка, рабочая заметка или документы. '
                             f'Сравните версии командой compare --request {request_id} и подайте заявку заново.')
        accepted = dict(request, status='accepted', revision=number, note=_note_path(key, number),
                        result=result, evidence=evidence, task=task,
                        accepted_context=_related_records(vault, db, record), **checked)
        _archive(path, _render_revision(accepted))
        db.execute('INSERT INTO revision_history VALUES (?, ?, ?)', (key, number, _json(accepted)))
        db.execute("UPDATE revision_requests SET status='accepted' WHERE id=?", (request_id,))
        return accepted
