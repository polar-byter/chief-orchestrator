"""Operational task viewer with authenticated, project-scoped user controls."""
from __future__ import annotations

import argparse
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit
import uuid

from . import journal
from . import tracking, observations, delivery, stage_monitor
from . import native_team_routes
from ..native_team import team_for
from ..project_vault import ProjectVault
from .resource_observer import ResourceObserver
from .workspace_navigation import WorkspaceNavigation
from .stage_documents import DocumentConflict, read_document
from ..stage_collaboration import discussion_risk_source
from .launcher import SERVICE, VERSION, project_id, read_state, runtime_folder, write_state
from ..task_metrics import TaskMetrics, corrected_events
from .. import task_control_state
from .. import localization
from .controls import TaskControls
from .quota_monitor import QuotaMonitor

PAGE = Path(__file__).resolve().parent / 'static' / 'runtime.html'


def dashboard_display_text(root: Path, source: str) -> str:
    """Подставляет название среды в текст интерфейса до загрузки данных пользователя."""
    from ..runtime_selection import uses_pi_runtime
    return source.replace('OpenCode', 'Pi') if uses_pi_runtime(root) else source


def task_directory(root: Path, task: str) -> Path:
    directory = TaskMetrics(root).task_dir(task)
    if directory.resolve().parent != (root / '.metrics').resolve():
        raise ValueError('Task directory is outside the project journal folder')
    if not (directory / 'events.jsonl').is_file():
        raise FileNotFoundError('Task not found')
    if (directory / 'events.jsonl').resolve().parent != directory.resolve():
        raise ValueError('Journal is outside the task directory')
    return directory


def task_view(root: Path, task: str, reader=None, *, summary_only=False) -> dict:
    events = corrected_events(journal.read_events(task_directory(root, task) / 'events.jsonl'))
    result = journal.brief_summary(events)
    result.update(task=task, started_at=events[0]['at'], session=events[0].get('session'))
    team = team_for(root, task)
    activity_fields = ('kind', 'at') if team is not None else (
        'kind', 'at', 'work_item', 'result', 'scope', 'status', 'reason')
    result['activity'] = [
        {key: event[key] for key in activity_fields if key in event}
        for event in events[-30:]
    ]
    if team is not None:
        # Здесь источником служит локальная команда. Обращение к чужому шлюзу могло бы
        # подмешать в экран сообщения и модели другой среды.
        native = team.operator_view(include_jobs=False) if summary_only else native_team_routes.public_team(team)
        if result['status'] == 'finished':
            native['status'] = 'finished'
        if not summary_only:
            from ..native_review_report import review_counts
            counts = review_counts(native['jobs'])
            result['work_runs'] += counts['reviews']
            result['green_work_runs'] += counts['passed']
            result['reworks'] += counts['reworks']
        control = native_team_routes.control_view(native)
        result.update(native_team=native, control=control, status=control['status'])
        return result
    try:
        control = task_control_state.read_state(root, task)
        result['control'] = control
        if control.get('status') and control['status'] != 'running':
            result['status'] = control['status']
    except (OSError, ValueError):
        result['control'] = {'status': 'error', 'error': 'Не удалось прочитать состояние управления задачей'}
        result['status'] = 'error'
    if reader is not None:
        try:
            live = reader.view(root, events)
        except (OSError, ValueError, TypeError, KeyError):
            live = {'available': False, 'reason': 'Сведения сессии временно недоступны'}
        try:
            result['tokens'] = observations.token_view(events, live)
        except (ValueError, TypeError):
            result['tokens'] = {'available': False, 'reason': 'Не удалось прочитать детализацию токенов'}
        work = {'items': [], 'progress': {'available': False}, 'sessions': live,
                'configured_models': {}, 'architecture': None, 'errors': []}
        sources = (
            ('items', lambda: tracking.work_items(events), 'Не удалось прочитать задания'),
            ('architecture', lambda: tracking.architecture(root, task), 'Не удалось прочитать архитектуру'),
            ('progress', lambda: tracking.function_progress(root, task, events), 'Не удалось прочитать реестр функций'),
            ('configured_models', lambda: observations.display_models(root), 'Не удалось прочитать настройки моделей'),
        )
        for field, read, error in sources:
            try:
                work[field] = read()
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                work['errors'].append(error)
        if work['architecture']:
            work['architecture'] = {key: value for key, value in work['architecture'].items() if key != 'text'}
        try:
            finished = next((e for e in reversed(events) if e['kind'] == 'finish'), None)
            if finished and finished.get('usage', {}).get('session_usage'):
                live = dict(live, usage=finished['usage'])
            observations.attach_models(work['items'], live)
        except (ValueError, TypeError, KeyError, AttributeError):
            work['errors'].append('Не удалось сопоставить фактические модели')
        if work['errors']:
            work['error'] = '. '.join(work['errors'])
        result['work'] = work
        try:
            result['completion'] = delivery.completion(root, task, events)
        except (OSError, ValueError, TypeError, KeyError):
            result['completion'] = {'finished': False}
        if result['status'] in ('cancelled', 'deleting', 'error'):
            result['completion'] = {'finished': False}
    return result


def task_list(root: Path) -> list[dict]:
    folder = root / '.metrics'
    if not folder.is_dir():
        return []
    rows = []
    for directory in folder.iterdir():
        if not directory.is_dir() or directory.name.startswith('.'):
            continue
        try:
            view = task_view(root, directory.name, summary_only=True)
            rows.append({key: view[key] for key in ('task', 'title', 'status', 'started_at')})
        except (OSError, ValueError, sqlite3.Error):
            continue
    return sorted(rows, key=lambda row: (row['started_at'], row['task']), reverse=True)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, port: int = 0, instance: str | None = None):
        self.root = Path(root).resolve()
        self.instance = instance or uuid.uuid4().hex
        self.control_token = secrets.token_urlsafe(32)
        self.ui_token = secrets.token_urlsafe(32)
        self.reader = observations.SessionReader()
        self.controls = TaskControls(self.root)
        self.quota_monitor = QuotaMonitor(self.root, self.reader, self.controls)
        self.configurator = None
        self.configurator_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), Handler)
        self.resource_observer = ResourceObserver(self.root, WorkspaceNavigation(self.state()['base_url']))

    def serve_forever(self, poll_interval=.5):
        self.quota_monitor.start()
        self.resource_observer.start()
        try:
            return super().serve_forever(poll_interval)
        finally:
            self.resource_observer.stop()
            self.quota_monitor.stop()

    def open_configurator(self) -> dict:
        with self.configurator_lock:
            if self.configurator is not None and self.configurator.poll() is None:
                return {'status': 'started', 'reused': True, 'pid': self.configurator.pid}
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            folder = runtime_folder(self.root)
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / 'configurator.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, '-u', '-m', 'kit.configurator.app'],
                                           cwd=self.root, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=log, **options)
            self.configurator = process
            try:
                code = process.wait(timeout=0.35)
            except subprocess.TimeoutExpired:
                return {'status': 'started', 'reused': False, 'pid': process.pid}
            raise RuntimeError('Не удалось запустить конфигуратор; подробности в .metrics/.dashboard/configurator.log')

    def open_project(self) -> dict:
        """Open this server's project folder; never execute a launch script."""
        if not self.root.is_dir():
            raise FileNotFoundError('Project folder no longer exists')
        if os.name == 'nt':
            os.startfile(str(self.root))
        else:
            program = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([program, str(self.root)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {'opened': True}

    def state(self) -> dict:
        return {'service': SERVICE, 'version': VERSION, 'instance_id': self.instance,
                'pid': os.getpid(), 'project_root': str(self.root),
                'base_url': f'http://127.0.0.1:{self.server_port}',
                'control_token': self.control_token}

    def health(self) -> dict:
        result = self.state()
        del result['control_token']
        result['project'] = {'id': project_id(self.root), 'name': self.root.name}
        result['poll_interval_ms'] = 2000
        return result


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_request(self, code='-', size='-'):
        if str(code).isdigit() and int(code) >= 400:
            super().log_request(code, size)

    def host_allowed(self) -> bool:
        host = self.headers.get('Host', '').lower()
        return host in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def send_body(self, status: int, body: bytes, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Language', self.language())
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, data: dict | list):
        if isinstance(data, dict):
            data = dict(data)
            for key in ('error', 'message'):
                if key in data:
                    data[key] = localization.diagnostic(data[key], self.language())
        self.send_body(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def language(self):
        query = parse_qs(urlsplit(self.path).query)
        if 'lang' in query:
            return localization.choose_language(query['lang'])
        return localization.request_language(self.headers.get('Accept-Language'), localization.load_language(self.server.root))

    def selected_task(self, query: dict) -> str:
        values = query.get('task', [])
        if len(values) != 1 or not values[0]:
            raise ValueError('One task ID is required')
        project = query.get('project', [])
        if project and project != [project_id(self.server.root)]:
            raise ValueError('This server belongs to a different project')
        return values[0]

    def stage_parameter(self, query, name, *, required=True):
        values = query.get(name)
        if values is None and not required:
            return None
        if not values or len(values) != 1 or not values[0]:
            raise ValueError(f'Нужно одно значение параметра {name}.')
        return values[0]

    def read_stage(self, path, query):
        parameter = lambda name: self.stage_parameter(query, name)
        root = self.server.root
        if path == '/api/stage':
            data = stage_monitor.stage_view(root, self.stage_parameter(query, 'stage', required=False))
            self.send_json(200, data)
        elif path == '/api/stage/runs':
            events = stage_monitor.run_events(root, parameter('task'))
            self.send_json(200, {'runs': [{k: e.get(k) for k in stage_monitor.RUN_FIELDS} for e in events]})
        elif path == '/api/stage/resource':
            self.send_json(200, stage_monitor.resource_inputs(root, parameter('stage'), parameter('version')))
        elif path == '/api/stage/document':
            self.send_json(200, read_document(ProjectVault.load(root), parameter('stage'), parameter('feature'),
                                              parameter('section'), self.stage_parameter(query, 'revision', required=False)))
        elif path == '/api/stage/discussion-source':
            note = discussion_risk_source(ProjectVault.load(root), parameter('stage'), parameter('thread'))
            self.send_body(200, note.encode('utf-8'), 'text/plain; charset=utf-8')
        elif path == '/api/stage/source':
            note = stage_monitor.source(ProjectVault.load(root), parameter('stage'), parameter('feature'),
                                        parameter('section'), self.stage_parameter(query, 'revision', required=False))
            self.send_body(200, note.encode('utf-8'), 'text/plain; charset=utf-8')
        else:
            log = stage_monitor.run_log(root, parameter('task'), parameter('event'))
            self.send_body(200, log.encode('utf-8'), 'text/plain; charset=utf-8')

    def change_stage(self, path):
        origin = self.headers.get('Origin', '')
        allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
        token = self.headers.get('X-Dashboard-UI-Token', '')
        if origin not in allowed or not token or not secrets.compare_digest(token, self.server.ui_token):
            self.send_json(403, {'error': 'Недостаточно прав на изменение этапа.'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = 6 * 1024**2 + 4096 if path == '/api/stage/document' else 65536
            if (not 0 < length <= limit or self.headers.get('Transfer-Encoding') or urlsplit(self.path).query
                    or self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                raise ValueError('Ожидается команда JSON для одного этапа.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Укажите поля команды.')
            result = stage_monitor.command(self.server.root, data, path.rsplit('/', 1)[-1])
            self.send_json(200, result)
        except DocumentConflict as error:
            self.send_json(409, {'error': str(error)})
        except (ValueError, UnicodeError) as error:
            self.send_json(400, {'error': str(error)})
        except (OSError, sqlite3.Error) as error:
            self.send_json(503, {'error': f'Не удалось записать команду в базу этапа. {error}'})

    def do_GET(self):
        if not self.host_allowed():
            self.send_json(403, {'error': 'Forbidden Host'})
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query, keep_blank_values=True)
        try:
            if parts.path == '/':
                body = dashboard_display_text(self.server.root, PAGE.read_text(encoding='utf-8')).encode('utf-8')
                status, content_type = 200, 'text/html; charset=utf-8'
            elif parts.path in ('/localization.js', '/native-team.js'):
                body = PAGE.with_name(parts.path[1:]).read_bytes()
                status, content_type = 200, 'text/javascript; charset=utf-8'
            elif parts.path in ('/stage', '/stage.js', '/stage.css', '/stage-decisions.js',
                                 '/stage-resources.js', '/stage-resources.css', '/stage-workspace.js', '/stage-workspace.css'):
                name = 'stage.html' if parts.path == '/stage' else parts.path[1:]
                media = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}
                asset = PAGE.with_name(name)
                body = asset.read_bytes()
                status, content_type = 200, media[asset.suffix] + '; charset=utf-8'
            elif parts.path in ('/api/stage', '/api/stage/source', '/api/stage/runs', '/api/stage/log',
                                '/api/stage/resource', '/api/stage/document', '/api/stage/discussion-source'):
                self.read_stage(parts.path, query)
                return
            elif parts.path == '/api/i18n':
                body = json.dumps({'saved_language': localization.saved_language(self.server.root),
                                   'catalog': {dashboard_display_text(self.server.root, key): dashboard_display_text(self.server.root, value)
                                               for key, value in localization.catalog().items()}}, ensure_ascii=False).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/health':
                body = json.dumps(self.server.health(), ensure_ascii=False).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/chief-mode':
                if query:
                    raise ValueError('Состояние режима читается для открытого проекта.')
                body = json.dumps(native_team_routes.public_mode(self.server.root)).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/tasks':
                body = json.dumps(task_list(self.server.root), ensure_ascii=False).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/task':
                task = self.selected_task(query)
                self.server.controls.view(task)
                data = localization.task_presentation(task_view(self.server.root, task, self.server.reader), self.language())
                body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/ui':
                body = json.dumps({'token': self.server.ui_token}).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path == '/api/task/control':
                body = json.dumps(self.server.controls.view(self.selected_task(query)), ensure_ascii=False).encode('utf-8')
                status, content_type = 200, 'application/json; charset=utf-8'
            elif parts.path in ('/architecture', '/launch-file'):
                task = self.selected_task(query)
                task_directory(self.server.root, task)
                if parts.path == '/launch-file':
                    target = delivery.launch_file(self.server.root, task)
                    document = {'document': target.relative_to(self.server.root).as_posix(), 'text': target.read_text(encoding='utf-8-sig')} if target else None
                    heading = 'Файл запуска'
                else:
                    document = tracking.architecture(self.server.root, task)
                    heading = 'Архитектура кода'
                if document is None:
                    raise FileNotFoundError('Документ ещё не опубликован')
                title = html.escape(document['document'])
                text = html.escape(document['text'])
                language = self.language()
                heading = localization.diagnostic(heading, language)
                body = (f'<!doctype html><html lang="{language}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>{heading} · {title}</title><style>body{{max-width:1000px;margin:30px auto;padding:0 20px;background:#101724;color:#e8edf5;font:16px system-ui}}'
                        f'pre{{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.6}}a{{color:#9ec8ff}}</style><h1>{heading}</h1><p>{title}</p><pre>{text}</pre></html>').encode('utf-8')
                status, content_type = 200, 'text/html; charset=utf-8'
            elif parts.path == '/api/report':
                task = self.selected_task(query)
                folder = task_directory(self.server.root, task)
                report = folder / 'report.md'
                if report.resolve().parent != folder.resolve():
                    raise ValueError('Report is outside the task directory')
                metrics = TaskMetrics(self.server.root)
                events = metrics.read(task)
                finished = next((event for event in events if event['kind'] == 'finish'), None)
                if finished and isinstance(finished.get('usage'), dict):
                    content = metrics.render(task, events, finished['usage'], finished.get('ended', finished['at']), self.language())
                elif report.stat().st_size <= 2 * 1024 * 1024:
                    content = report.read_text(encoding='utf-8-sig')
                else:
                    raise ValueError('Report exceeds 2 MiB')
                body = content.encode('utf-8')
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError('Report exceeds 2 MiB')
                status, content_type = 200, 'text/plain; charset=utf-8'
            else:
                body = b'{"error":"Not found"}'
                status, content_type = 404, 'application/json; charset=utf-8'
        except FileNotFoundError as error:
            body = json.dumps({'error': localization.diagnostic(str(error), self.language())}, ensure_ascii=False).encode('utf-8')
            status, content_type = 404, 'application/json; charset=utf-8'
        except ValueError as error:
            body = json.dumps({'error': localization.diagnostic(str(error), self.language())}, ensure_ascii=False).encode('utf-8')
            status, content_type = 400, 'application/json; charset=utf-8'
        except (OSError, sqlite3.Error) as error:
            print(f'Dashboard read failed: {error}', file=sys.stderr, flush=True)
            body = json.dumps({'error': localization.diagnostic('Cannot read dashboard data', self.language())}, ensure_ascii=False).encode('utf-8')
            status, content_type = 500, 'application/json; charset=utf-8'
        self.send_body(status, body, content_type)

    def do_POST(self):
        if not self.host_allowed():
            self.send_json(403, {'error': 'Forbidden Host'})
            return
        path = urlsplit(self.path).path
        if path in ('/api/chief-mode', '/api/native-team', '/api/native-partial'):
            self.change_native(path)
            return
        if path in ('/api/stage/action', '/api/stage/decision', '/api/stage/resource', '/api/stage/document'):
            self.change_stage(path)
            return
        if path == '/api/language':
            origin = self.headers.get('Origin', '')
            allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            token = self.headers.get('X-Dashboard-UI-Token', '')
            if origin not in allowed or not token or not secrets.compare_digest(token, self.server.ui_token):
                self.send_json(403, {'error': 'Forbidden settings request'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 or self.headers.get('Transfer-Encoding') or urlsplit(self.path).query:
                    raise ValueError('Invalid language preference request')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON required')
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict) or set(value) != {'language'} or not isinstance(value['language'], str):
                    raise ValueError('Language preference must contain only language')
                localization.save_language(self.server.root, value['language'])
                self.send_json(200, {'language': value['language']})
            except (ValueError, UnicodeError) as error:
                self.send_json(400, {'error': str(error)})
            except OSError:
                self.send_json(503, {'error': 'Could not save the language preference.'})
            return
        if urlsplit(self.path).path == '/api/task/control':
            origin = self.headers.get('Origin', '')
            allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            token = self.headers.get('X-Dashboard-UI-Token', '')
            if origin not in allowed or not token or not secrets.compare_digest(token, self.server.ui_token):
                self.send_json(403, {'error': 'Forbidden task control request'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 or self.headers.get('Transfer-Encoding') or urlsplit(self.path).query:
                    raise ValueError('Invalid task control request')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON required')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {'task', 'action'} or not all(isinstance(v, str) for v in data.values()):
                    raise ValueError('Only task and action are accepted')
                self.send_json(202, self.server.controls.request(data['task'], data['action']))
            except (ValueError, UnicodeError) as error:
                self.send_json(400, {'error': str(error)})
            except (OSError, RuntimeError, sqlite3.Error):
                self.send_json(503, {'error': 'Не удалось сохранить действие управления задачей'})
            return
        if urlsplit(self.path).path in ('/api/models/open', '/api/project/open'):
            origin = self.headers.get('Origin', '')
            allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            token = self.headers.get('X-Dashboard-UI-Token', '')
            if origin not in allowed or not token or not secrets.compare_digest(token, self.server.ui_token):
                self.send_json(403, {'error': 'Forbidden settings request'})
                return
            if self.headers.get('Content-Length', '0') != '0' or urlsplit(self.path).query:
                self.send_json(400, {'error': 'This action accepts no parameters'})
                return
            try:
                result = self.server.open_configurator() if urlsplit(self.path).path == '/api/models/open' else self.server.open_project()
                self.send_json(200, result)
            except (OSError, RuntimeError):
                error = 'Не удалось открыть конфигуратор. Проверьте .metrics/.dashboard/configurator.log' if urlsplit(self.path).path == '/api/models/open' else 'Не удалось открыть папку проекта'
                self.send_json(503, {'error': error})
            return
        if urlsplit(self.path).path != '/api/stop':
            self.send_json(404, {'error': 'Not found'})
            return
        supplied = self.headers.get('X-Dashboard-Token', '')
        if not supplied or not secrets.compare_digest(supplied, self.server.control_token):
            self.send_json(403, {'error': 'Invalid dashboard token'})
            return
        self.send_json(200, {'stopping': True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def change_native(self, path):
        origin = self.headers.get('Origin', '')
        allowed = (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
        token = self.headers.get('X-Dashboard-UI-Token', '')
        if origin not in allowed or not token or not secrets.compare_digest(token, self.server.ui_token):
            self.send_json(403, {'error': 'Недостаточно прав для управления проектом.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if (not 0 < size <= 4096 or self.headers.get('Transfer-Encoding') or urlsplit(self.path).query
                    or self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                raise ValueError('Ожидается команда JSON без параметров URL.')
            result = native_team_routes.command(self.server.root, path, json.loads(self.rfile.read(size)))
            self.send_json(200, result)
        except (ValueError, UnicodeError) as error:
            self.send_json(400, {'error': str(error)})
        except (OSError, RuntimeError, sqlite3.Error):
            self.send_json(503, {'error': 'Команда не подтверждена. Обновите состояние перед повтором.'})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--instance', default=None)
    args = parser.parse_args(argv)
    if not args.project.is_dir():
        parser.error('Project directory does not exist')
    server = None
    try:
        server = DashboardServer(args.project, args.port, args.instance)
        write_state(server.root, server.state())
        print('Dashboard: ' + server.state()['base_url'], flush=True)
        server.serve_forever(poll_interval=0.1)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as error:
        print('Dashboard: ' + str(error), file=sys.stderr, flush=True)
        return 1
    finally:
        if server is not None:
            server.server_close()
            state = read_state(server.root)
            if state and state.get('instance_id') == server.instance and state.get('pid') == os.getpid():
                (runtime_folder(server.root) / 'runtime.json').unlink(missing_ok=True)


if __name__ == '__main__':
    raise SystemExit(main())
