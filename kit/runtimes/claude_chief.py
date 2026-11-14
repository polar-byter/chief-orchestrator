"""Команды Claude Code и факты одного запуска из его потока stream-json."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import UUID


def _executable() -> str | None:
    configured = os.environ.get('CHIEF_CLAUDE_EXECUTABLE')
    if configured:
        path = Path(configured)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    name = 'claude.exe' if os.name == 'nt' else 'claude'
    found = shutil.which(name)
    if found:
        return found
    personal = Path.home() / '.local' / 'bin' / name
    if personal.is_file() and os.access(personal, os.X_OK):
        return str(personal)
    return shutil.which('claude.cmd') if os.name == 'nt' else None


def _inspect(executable, flag):
    result = subprocess.run([executable, flag], stdin=subprocess.DEVNULL, capture_output=True,
                            encoding='utf-8', errors='replace', timeout=5,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError(f'Claude Code {flag} завершился с кодом {result.returncode}.')
    return result.stdout.strip()


def probe() -> dict:
    """Читает версию и справку установленного CLI. Сессия и модель для этого не нужны."""
    executable = _executable()
    capabilities = dict(new_session=False, resume_by_id=False, stdin_prompt=False, json_events=False)
    status = dict(runtime='claude', available=False, executable=executable, version=None, error=None,
                  capabilities=capabilities, integration_scope='chief_turn',
                  # Печатный режим доставляет ход Чифу. Разрешения инструментов не подтверждают изоляцию семьи процессов.
                  worker_guarantees=dict(assigned_file_only=False, family_stop_confirmed=False,
                                         family_queue_control=False, family_export=False, family_crash_reconciliation=False))
    if not executable:
        status['error'] = 'Команда claude не найдена. Проверьте PATH и путь CHIEF_CLAUDE_EXECUTABLE.'
        return status
    try:
        version = _inspect(executable, '--version')
        status['version'] = version.splitlines()[0] if version else None
        help_text = _inspect(executable, '--help')
    except subprocess.TimeoutExpired:
        status['error'] = 'Claude Code не ответил на запрос версии или справки за 5 секунд.'
        return status
    except (OSError, ValueError) as error:
        status['error'] = str(error)
        return status
    flags = set(re.findall(r'(?<![\w-])--[a-z][a-z-]*', help_text))
    capabilities.update(new_session={'--print', '--session-id'} <= flags,
                        resume_by_id='--resume' in flags, stdin_prompt='--input-format' in flags,
                        json_events={'--output-format', '--verbose'} <= flags and 'stream-json' in help_text)
    status['available'] = bool(status['version']) and all(capabilities.values())
    if not status['available']:
        status['error'] = 'В справке Claude Code отсутствуют команды для полного цикла запуска и продолжения сессии.'
    return status


def _session_id(value):
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    return str(parsed) if str(parsed) == value.lower() else None


def command(root: Path, *, session: str | None, resume: bool) -> list[str]:
    """Вызывающий процесс задаёт cwd=root и пишет запрос в stdin, затем закрывает канал."""
    if not root.is_dir():
        raise ValueError('Каталог проекта Claude Code отсутствует.')
    if resume and not session:
        raise ValueError('Для продолжения укажите UUID сессии Claude Code.')
    session_id = _session_id(session) if session is not None else None
    if session is not None and session_id is None:
        raise ValueError('Нужен UUID сессии Claude Code в формате с дефисами.')
    executable = _executable()
    if not executable:
        raise FileNotFoundError('Команда claude не найдена. Проверьте установку Claude Code и PATH.')
    args = [executable, '-p', '--input-format', 'text', '--output-format', 'stream-json', '--verbose']
    if session_id:
        args.extend(['--resume' if resume else '--session-id', session_id])
    return args


def parse_event(event: dict) -> dict:
    """Итоги и расход приходят в result. Промежуточные ответы могут относиться к подзадачам."""
    if not isinstance(event, dict) or event.get('parent_tool_use_id') or event.get('agent_id'):
        return {}
    kind, subtype = event.get('type'), event.get('subtype')
    session = _session_id(event.get('session_id'))
    parsed = {'session': session} if session else {}
    if kind == 'system' and subtype == 'init':
        return parsed
    if kind != 'result':
        return {}
    transport_usage = event.get('usage')
    if isinstance(transport_usage, dict):
        # Счётчик input_tokens у Anthropic не включает чтение и создание записей кэша.
        names = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
                 'cache_read_input_tokens': 'cached_input_tokens', 'cache_creation_input_tokens': 'cache_creation_input_tokens'}
        usage = {key: transport_usage[name] for name, key in names.items()
                 if type(transport_usage.get(name)) is int and transport_usage[name] >= 0}
        if usage:
            parsed['usage'] = usage
    answer = event.get('result')
    if subtype == 'success' and event.get('is_error') is False:
        parsed['completed'] = True
        if isinstance(answer, str):
            parsed['text'] = answer
    else:
        errors = event.get('errors')
        message = '\n'.join(x for x in errors if isinstance(x, str) and x.strip()) if isinstance(errors, list) else ''
        parsed.update(failed=True, error=message or (answer if isinstance(answer, str) and answer else None)
                      or 'Claude Code завершил запрос без успешного результата (' + str(subtype) + ').')
    return parsed
