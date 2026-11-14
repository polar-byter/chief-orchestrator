"""Приём отмеченных в Obsidian заявок без расширений и сетевых служб."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from .project_dispatch import dispatch_request
from .native_chief import check_resume
from .project_revisions import marked_for_submission, request_revision
from .project_vault import ProjectVault, publish_new
from .runtimes.pi_rpc import process_identity
from .task_control_state import operation_lock, plain_path
from .workspace_estimates import refresh_estimates
from .project_overview import OverviewCache, refresh_overview


POLL_SECONDS = 2
WATCHER_VERSION = 3
_children: dict[int, subprocess.Popen] = {}


def _prepare_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS inbox_marks (key TEXT PRIMARY KEY, checked INTEGER NOT NULL)')
    db.execute('''CREATE TABLE IF NOT EXISTS inbox_receipts (
        id TEXT PRIMARY KEY, key TEXT NOT NULL,
        stamp TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, request_id TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS inbox_deliveries (
        request_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, delivery TEXT NOT NULL)''')


def _start_receipt(db, key, request_id=None):
    identifier = uuid4().hex
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    db.execute("INSERT INTO inbox_receipts VALUES (?, ?, ?, 'started', '', ?)",
               (identifier, key, stamp, request_id))
    return identifier


def _claim(vault, key: str, marked: bool):
    """Отмечает переход от пустого флажка к установленному до обращения к агенту."""
    with vault.locked() as db:
        _prepare_tables(db)
        previous = db.execute('SELECT checked FROM inbox_marks WHERE key=?', (key,)).fetchone()
        if bool(previous and previous[0]) == marked:
            return None
        db.execute('INSERT OR REPLACE INTO inbox_marks VALUES (?, ?)', (key, int(marked)))
        if not marked:
            return None
        return _start_receipt(db, key)


def _record_outcome(vault, identifier, status, message, request_id):
    with vault.locked() as db:
        db.execute('UPDATE inbox_receipts SET status=?, message=?, request_id=? WHERE id=?',
                   (status, message, request_id, identifier))


def _send(vault, request, identifier=None, *, report_failure=True, resume=None):
    request_id = request['id']
    key = request['key']
    with vault.locked() as db:
        _prepare_tables(db)
        previous = db.execute('SELECT delivery FROM inbox_deliveries WHERE request_id=?', (request_id,)).fetchone()
        if previous:
            confirmed = json.loads(previous[0])
            check_resume(confirmed, resume)
            if identifier:
                db.execute("DELETE FROM inbox_receipts WHERE id=? AND status='started'", (identifier,))
            return confirmed
        if identifier is None:
            identifier = _start_receipt(db, key, request_id)
        else:
            db.execute('UPDATE inbox_receipts SET request_id=? WHERE id=?', (request_id, identifier))
    try:
        delivery = dispatch_request(vault, request, resume=resume)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        _record_outcome(vault, identifier, 'Требует внимания', str(error), request_id)
        if report_failure:
            raise
        return None
    message = f"Служба подтвердила получение. Сессия Чифа: `{delivery['session']}`."
    if delivery.get('status') == 'completed':
        result = plain_path(vault.root / '.metrics' / request['task_id'] / 'native-turn.json')
        message = (f"CLI {delivery['agent']} завершил ход. Сессия Чифа: `{delivery['session']}`. "
                   f"[Ответ и фактические показатели]({result.as_uri()}).")
    with vault.locked() as db:
        # После обновления базы подтверждение могло быть опубликовано прежней версией.
        old = db.execute("SELECT id FROM inbox_receipts WHERE request_id=? AND message=? AND status='published'",
                         (request_id, message)).fetchone()
        if old:
            db.execute('DELETE FROM inbox_receipts WHERE id=?', (identifier,))
            identifier = old[0]
        else:
            db.execute("UPDATE inbox_receipts SET status='Передано Чифу', message=? WHERE id=?", (message, identifier))
        db.execute('INSERT INTO inbox_deliveries VALUES (?, ?, ?)',
                   (request_id, identifier, json.dumps(delivery, ensure_ascii=False)))
    return delivery


def retry_delivery(vault, request, *, resume=None):
    """Повторяет доставку открытой заявки и публикует её новый результат в Obsidian."""
    with operation_lock(vault.root, 'chief-inbox-scan'):
        try:
            return _send(vault, request, resume=resume)
        finally:
            _publish_outcomes(vault, required_request=request['id'])


def _deliver(vault, key: str, content: bytes, identifier: str):
    try:
        request = request_revision(vault, key, expected_workspace=content)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        _record_outcome(vault, identifier, 'Требует внимания', str(error), None)
        return
    if request.get('status') == 'accepted':
        _record_outcome(vault, identifier, 'Готово', 'Изменение уже принято. Откройте историю редакций.', request['id'])
        return
    _send(vault, request, identifier, report_failure=False)


def _receipt_path(vault, row):
    return plain_path(vault.path / 'Квитанции' / row['key'] / f"{row['stamp']}-{row['id']}.md")


def _publish_outcomes(vault, *, required_request=None):
    publication_error = None
    with vault.locked() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox_receipts'").fetchone():
            return []
        rows = list(db.execute("SELECT * FROM inbox_receipts WHERE status <> 'published' ORDER BY rowid"))
        paths = []
        for row in rows:
            status, message, request_id = row['status'], row['message'], row['request_id']
            if status == 'started':
                # При перезапуске неизвестно, успел ли ответить агент. Повтор решает оператор.
                status, message = 'Передача прервана', 'Проверьте список заявок и журнал задачи перед повторной отправкой.'
            text = f"# {status}\n\nЗапись `{row['key']}`.\n\n{message}\n"
            if request_id:
                text += f'\nЗаявка `{request_id}`.\n'
                if status not in ('Передано Чифу', 'Готово'):
                    python = r'.\kit\python.cmd' if os.name == 'nt' else 'sh kit/python.sh'
                    text += f'\nПовтор из каталога проекта после проверки службы:\n\n```\n{python} -m kit.project_docs send --request {request_id}\n```\n'
            else:
                text += '\nДля новой подачи снимите отметку, дождитесь сохранения и снова отметьте «Передать Чифу».\n'
            text += f"\n[[История/{row['key']}/Редакции|История редакций]]\n"
            encoded = text.encode('utf-8')
            try:
                path = _receipt_path(vault, row)
                if not publish_new(path, encoded) and path.read_bytes() != encoded:
                    # Сохраняем исходную заметку и выдаём другое имя для квитанции.
                    path = path.with_stem(path.stem + '-квитанция')
                    if not publish_new(path, encoded) and path.read_bytes() != encoded:
                        raise ValueError(f'Имя квитанции занято: {path}')
            except (OSError, ValueError) as error:
                print(f"Запись {row['key']}: {error}", file=sys.stderr, flush=True)
                if required_request and request_id == required_request:
                    publication_error = error
                continue
            db.execute("UPDATE inbox_receipts SET status='published' WHERE id=?", (row['id'],))
            paths.append(path)
    if publication_error is not None:
        raise ValueError(f'Результат сохранён в базе. Не удалось записать квитанцию: {publication_error}') from publication_error
    return paths


def process_inbox(vault: ProjectVault, *, cache=None) -> list[Path]:
    """Передаёт отмеченные записи при включённом режиме и возвращает квитанции."""
    from .chief_mode import mode_state
    changed = []
    for key, workspace in vault.workspaces().items():
        try:
            info = workspace.stat()
        except OSError:
            if cache is not None:
                cache.pop(key, None)
            continue
        signature = (info.st_mtime_ns, info.st_size, info.st_ino)
        if cache is None or cache.get(key) != signature:
            changed.append((key, workspace, signature))
    # В простое достаточно одного запроса каталога и stat заметок. Инспекция
    # после старта также восстанавливает квитанцию прерванной передачи.
    if cache is not None and None in cache and not changed:
        return []
    if not mode_state(vault.root)['enabled']:
        return []
    with operation_lock(vault.root, 'chief-inbox-scan'):
        paths = _publish_outcomes(vault)
        for key, workspace, signature in changed:
            try:
                content = plain_path(workspace).read_bytes()
            except (ValueError, OSError) as error:
                print(f'Запись {key}: {error}', file=sys.stderr, flush=True)
                if cache is not None:
                    cache[key] = signature
                continue
            try:
                marked = marked_for_submission(content.decode('utf-8-sig'))
            except (UnicodeError, ValueError):
                # Пользователь может собирать заметку из частей. Без целого заголовка заявки нет.
                pass
            else:
                identifier = _claim(vault, key, marked)
                if identifier:
                    _deliver(vault, key, content, identifier)
                    paths.extend(_publish_outcomes(vault))
            if cache is not None:
                cache[key] = signature
        if cache is not None:
            cache[None] = True
        return paths


def _service_folder(vault) -> Path:
    folder = plain_path(vault.root / '.metrics' / '.documentation-inbox')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _state(vault) -> dict:
    path = plain_path(_service_folder(vault) / 'watcher.json')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or value.get('project') != vault.project_id:
        raise ValueError(f'Неизвестное состояние наблюдателя: {path}')
    return value


def _write_state(vault, value):
    target = plain_path(_service_folder(vault) / 'watcher.json')
    temp = target.with_name(uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _alive(state: dict) -> bool:
    return (type(state.get('pid')) is int and state['pid'] > 0 and bool(state.get('identity'))
            and process_identity(state['pid']) == state['identity'])


def ensure_inbox_watcher(vault: ProjectVault, *, only_if_running=False) -> dict:
    """Запускает один локальный наблюдатель и проверяет готовность именно этого процесса."""
    folder = _service_folder(vault)
    for pid, child in list(_children.items()):
        if child.poll() is not None:
            _children.pop(pid, None)
    with operation_lock(vault.root, 'chief-inbox-start'):
        state = _state(vault)
        running = _alive(state) and not state.get('stop')
        if only_if_running and not running:
            return {}
        if (running and state.get('path') == str(vault.path)
                and state.get('watcher_version') == WATCHER_VERSION):
            return state
        token = uuid4().hex
        command = [sys.executable, '-B', '-m', 'kit.project_inbox', '--root', str(vault.root), '--instance', token]
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with plain_path(folder / 'watcher.log').open('ab') as log:
            process = subprocess.Popen(command, cwd=Path(__file__).parents[1], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, **options)
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                state = _state(vault)
                if state.get('instance') == token and _alive(state):
                    _children[process.pid] = process
                    return state
                time.sleep(.05)
        except BaseException:
            process.terminate()
            process.wait(timeout=5)
            raise
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        raise RuntimeError(f'Наблюдатель не запущен. Подробности в {folder / "watcher.log"}.')


def stop_inbox_watcher(vault: ProjectVault) -> None:
    """Просит наблюдателя завершить текущую операцию и выйти."""
    with operation_lock(vault.root, 'chief-inbox-start'):
        state = _state(vault)
        if not _alive(state):
            return
        state['stop'] = True
        _write_state(vault, state)
        child = _children.get(state['pid'])
        if child is not None:
            try:
                child.wait(timeout=POLL_SECONDS + 2)
            except subprocess.TimeoutExpired:
                # Передача агенту может занять дольше. Запрос остановки останется на диске.
                return
            _children.pop(state['pid'], None)


def _serve(root: Path, token: str) -> None:
    vault = ProjectVault.load(root)
    state = dict(project=vault.project_id, path=str(vault.path), instance=token, estimates=1, overview=1,
                 watcher_version=WATCHER_VERSION,
                 pid=os.getpid(), identity=process_identity(os.getpid()))
    _write_state(vault, state)
    previous_error = None
    cache = {}
    estimate_cache = {}
    overview_cache = OverviewCache()
    while True:
        current = _state(vault)
        if current.get('instance') != token or current.get('stop'):
            return
        try:
            refresh_estimates(vault, (row['key'] for row in vault.records()), estimate_cache)
            process_inbox(vault, cache=cache)
            refresh_overview(vault, overview_cache)
            previous_error = None
        except (OSError, ValueError, sqlite3.Error) as error:
            # При переносе базы прежний процесс больше не обслуживает этот каталог.
            try:
                current_vault = ProjectVault.load(root)
            except (OSError, ValueError, sqlite3.Error):
                return
            if current_vault != vault:
                return
            if str(error) != previous_error:
                print(str(error), file=sys.stderr, flush=True)
                previous_error = str(error)
        time.sleep(POLL_SECONDS)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--instance', required=True)
    args = parser.parse_args(argv)
    _serve(args.root, args.instance)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
