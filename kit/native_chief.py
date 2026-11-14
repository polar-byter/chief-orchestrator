"""Доставка одной сохранённой заявки в нативную сессию Codex или Claude."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from uuid import UUID, uuid4

from .native_chief_process import pause_direct_project, run_direct
from .project_vault import publish_new
from .runtimes import claude_chief, codex_chief, native_process
from .task_control_state import plain_path


TRANSPORTS = {'codex': codex_chief, 'claude': claude_chief}
TURN_TIMEOUT = 600
MAX_EVENT_BYTES = 4 * 1024 * 1024


def agent_for(vault) -> str:
    with vault.reading() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_agent'").fetchone():
            from .installer import project_agent
            selected = project_agent(vault.root)
            return selected if selected in TRANSPORTS else 'default'
        row = db.execute('SELECT agent FROM delivery_agent WHERE id=1').fetchone()
    agent = row[0] if row else 'default'
    if agent != 'default' and agent not in TRANSPORTS:
        raise ValueError('В базе указан неизвестный агент доставки.')
    return agent


def configure(vault, agent) -> dict:
    if agent is not None:
        if agent != 'default' and agent not in TRANSPORTS:
            raise ValueError('Агент доставки должен быть default, codex или claude.')
        with vault.locked() as db:
            db.execute('CREATE TABLE IF NOT EXISTS delivery_agent (id INTEGER PRIMARY KEY CHECK(id=1), agent TEXT NOT NULL)')
            db.execute('INSERT OR REPLACE INTO delivery_agent VALUES (1, ?)', (agent,))
    return dict(project=vault.project_id, agent=agent_for(vault))


def check_resume(receipt, resume):
    if resume is not None and (receipt.get('resume') != str(UUID(resume)) or receipt.get('agent') not in TRANSPORTS):
        raise ValueError('Эта заявка уже привязана к другому ходу. Проверьте её исходную квитанцию.')


def _publish(folder, name, data):
    path = plain_path(folder / name)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if not publish_new(path, encoded) and json.loads(path.read_text(encoding='utf-8')) != data:
        raise ValueError(f'Файл {name} содержит другую попытку доставки. Сверьте журнал заявки.')


def _sessions(db):
    db.execute('''CREATE TABLE IF NOT EXISTS native_sessions (
        session TEXT NOT NULL, agent TEXT NOT NULL, record_key TEXT NOT NULL, first_request TEXT NOT NULL, active_request TEXT,
        PRIMARY KEY(session, agent))''')


def _claim(vault, request, folder, attempt, prompt):
    with vault.locked() as db:
        row = db.execute('SELECT status FROM revision_requests WHERE id=?', (request['id'],)).fetchone()
        if not row or row[0] != 'pending':
            raise ValueError('Открытая заявка уже сменилась.')
        if db.execute('SELECT agent FROM delivery_agent WHERE id=1').fetchone()[0] != attempt['agent']:
            raise ValueError('Агент проекта изменился перед передачей. Повторите отправку.')
        _sessions(db)
        if attempt['resume']:
            changed = db.execute('UPDATE native_sessions SET active_request=? WHERE session=? AND agent=? '
                                 'AND record_key=? AND active_request IS NULL',
                                 (request['id'], attempt['resume'], attempt['agent'], request['key']))
            if changed.rowcount != 1:
                raise ValueError('Для продолжения нужна подтверждённая свободная сессия этой записи, агента и проекта.')
        _publish(folder, 'native-input.json', dict(project=vault.project_id, request=request['id'], prompt=prompt))
        _publish(folder, 'documentation-sending.json', attempt)


def _events(output, observe, finished):
    """Сохраняем UUID сразу после записи полной строки. Частичную строку дочитаем при опросе."""
    while True:
        offset = output.tell()
        line = output.readline(MAX_EVENT_BYTES + 1)
        if len(line) > MAX_EVENT_BYTES:
            raise ValueError('Одна запись CLI превысила предел в 4 МиБ.')
        if line and (line.endswith(b'\n') or finished):
            if line.strip():
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('CLI вернул событие в неизвестном формате.')
                observe(event)
        else:
            output.seek(offset)
            return


def _run(vault, request, folder, attempt, arguments, prompt, transport):
    result = dict(attempt, status='uncertain', session=None, exit_code=None)
    completed = failed = False

    def observe(event):
        nonlocal completed, failed
        facts = transport.parse_event(event)
        session = facts.get('session')
        if session:
            expected = attempt['session'] or result['session']
            if expected and session != expected:
                raise ValueError('CLI вернул UUID другой сессии.')
            if result['session'] is None:
                _publish(folder, 'native-session.json', dict(request=attempt['request'], session=session))
                result['session'] = session
        for key in ('text', 'error', 'usage'):
            if key in facts:
                result[key] = facts[key]
        if 'usage' in facts:
            result['usage_scope'] = 'session_reported_total' if attempt['agent'] == 'codex' else 'cli_result'
        completed |= bool(facts.get('completed'))
        failed |= bool(facts.get('failed'))

    def undo():
        with vault.locked() as db:
            db.execute('UPDATE native_sessions SET active_request=NULL WHERE active_request=?', (attempt['request'],))
            plain_path(folder / 'native-input.json').unlink()
            plain_path(folder / 'documentation-sending.json').unlink()
            if not any(folder.iterdir()):
                folder.rmdir()

    # Раздельные дескрипторы нужны, чтобы чтение событий не меняло позицию записи процесса.
    # Вывод инструментов бывает объёмным. После завершения сохраняем только подтверждённые факты.
    with TemporaryDirectory(prefix='chief-turn-') as temporary:
        path = Path(temporary) / 'stream'
        with path.open('wb') as stdout, path.open('rb') as stream, TemporaryFile() as stdin, TemporaryFile() as stderr:
            stdin.write(prompt.encode('utf-8') + b'\n')
            stdin.seek(0)
            try:
                code, interruption = run_direct(arguments, root=vault.root, folder=folder, attempt=attempt,
                    stdin=stdin, stdout=stdout, stderr=stderr, temporary=Path(temporary), timeout=TURN_TIMEOUT,
                    claim=lambda: _claim(vault, request, folder, attempt, prompt), undo=undo,
                    read_events=lambda finished: _events(stream, observe, finished))
                result['exit_code'] = code
                if interruption:
                    result['error'] = interruption
                elif completed and not failed and result['session'] and code == 0:
                    result['status'] = 'completed'
                    result.pop('error', None)
                elif failed:
                    result['status'] = 'failed'
            except (OSError, ValueError) as error:
                if not plain_path(folder / 'documentation-sending.json').exists():
                    raise
                result['error'] = str(error)
            if result['status'] != 'completed' and not result.get('error'):
                stderr.seek(max(0, stderr.seek(0, os.SEEK_END) - 2048))
                result['error'] = stderr.read().decode('utf-8', errors='replace').strip() or 'Нет подтверждения завершения хода.'
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def deliver(vault, request, folder, agent, resume, prompt):
    transport = TRANSPORTS[agent]
    if resume:
        resume = str(UUID(resume))
    session = resume or (str(uuid4()) if agent == 'claude' else None)
    arguments = transport.command(vault.root, session=session, resume=bool(resume))
    attempt = dict(format=1, request=request['id'], project=vault.project_id, key=request['key'],
                   task=request['task_id'], stage=request['stage'], base_revision=request['base_revision'],
                   agent=agent, session=session, resume=resume, started_at=datetime.now(timezone.utc).isoformat(),
                   prompt_sha256=hashlib.sha256(prompt.encode('utf-8')).hexdigest())
    owned = native_process.supported()
    if owned and agent == 'codex' and arguments[1:2] == ['exec']:
        # Новый клиент умеет отправлять работу уже запущенному общему демону.
        # Только отдельный сервер CLI станет дочерним процессом нашей группы.
        if '--no-daemon' in codex_chief._help(arguments[0], '--help'):
            arguments.insert(1, '--no-daemon')
        else:
            owned = False
    if owned:
        attempt['process_boundary'] = uuid4().hex
    result = _run(vault, request, folder, attempt, arguments, prompt, transport)
    _publish(folder, 'native-turn.json', result)
    return recover(vault, request, folder, attempt)


def recover(vault, request, folder, attempt):
    if (attempt['project'], attempt['task'], attempt['request']) != (vault.project_id, request['task_id'], request['id']):
        raise ValueError('Попытка доставки принадлежит другой заявке.')
    path = plain_path(folder / 'native-turn.json')
    if not path.is_file():
        raise ValueError(f'Результат передачи неизвестен. Сверьте журнал {folder}; повторный вызов CLI заблокирован.')
    result = json.loads(path.read_text(encoding='utf-8'))
    if any(result.get(key) != attempt[key] for key in ('project', 'task', 'request', 'agent', 'resume', 'key')):
        raise ValueError('Результат CLI относится к другой попытке.')
    if result['status'] != 'completed':
        if attempt['resume'] and result['status'] == 'failed':
            with vault.locked() as db:
                db.execute('UPDATE native_sessions SET active_request=NULL WHERE active_request=?', (request['id'],))
        raise ValueError(f"Ход {result['agent']} завершён со статусом {result['status']}: {result['error']}. Журнал: {path}")
    with vault.locked() as db:
        _sessions(db)
        if attempt['resume']:
            db.execute('UPDATE native_sessions SET active_request=NULL WHERE session=? AND agent=? AND active_request=?',
                       (result['session'], result['agent'], result['request']))
        else:
            db.execute('INSERT OR IGNORE INTO native_sessions VALUES (?, ?, ?, ?, NULL)',
                       (result['session'], result['agent'], request['key'], request['id']))
            owner = db.execute('SELECT first_request FROM native_sessions WHERE session=? AND agent=?',
                               (result['session'], result['agent'])).fetchone()
            if owner[0] != request['id']:
                raise ValueError('UUID сессии уже принадлежит другой исходной заявке.')
    _publish(folder, 'documentation-delivery.json', result)
    return result
