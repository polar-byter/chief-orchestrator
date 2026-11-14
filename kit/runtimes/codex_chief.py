"""Команда для одного хода Codex CLI и перевод его JSONL-событий в журнал Чифа."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID


def _executable():
    # cmd-обёртка npm меняет правила экранирования аргументов, особенно пути проекта.
    return shutil.which('codex.exe' if os.name == 'nt' else 'codex')


def _help(executable, *arguments):
    result = subprocess.run([executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True,
                            encoding='utf-8', errors='replace', timeout=8, check=True)
    return result.stdout.strip()


def probe() -> dict:
    """Сверяет синтаксис установленного CLI. Обращение к модели для этого не нужно."""
    executable = _executable()
    capabilities = dict(new_session=False, resume_by_id=False, stdin_prompt=False, json_events=False)
    info = dict(runtime='codex', available=False, executable=executable, version=None, error=None,
                capabilities=capabilities, integration_scope='chief_turn',
                # exec доставляет ход Чифу. Профиль среды и надзор за всем семейством требуют отдельного исполнителя.
                worker_guarantees=dict(assigned_file_only=False, family_stop_confirmed=False,
                                       family_queue_control=False, family_export=False, family_crash_reconciliation=False))
    if not executable:
        info['error'] = 'Исполняемый файл Codex CLI не найден в PATH.'
        return info
    try:
        info['version'] = _help(executable, '--version')
        start = _help(executable, 'exec', '--help')
        resume = _help(executable, 'exec', 'resume', '--help')
    except (OSError, subprocess.SubprocessError) as error:
        info['error'] = f'Не удалось проверить справку Codex CLI: {error}'
        return info
    capabilities.update(new_session='--cd' in start, resume_by_id='SESSION_ID' in resume,
                        stdin_prompt='stdin' in start and 'stdin' in resume,
                        json_events='--json' in start and '--json' in resume)
    info['available'] = all(capabilities.values())
    if not info['available']:
        info['error'] = 'В справке Codex CLI отсутствует часть команд для запуска и продолжения хода.'
    return info


def _thread_id(value):
    try:
        return str(UUID(str(value)))
    except ValueError as error:
        raise ValueError('Для продолжения Codex нужен UUID исходной сессии.') from error


def command(root: Path, *, session: str | None, resume: bool) -> list[str]:
    """Текст запроса передаётся по stdin. Права и модель выбирает конфигурация Codex."""
    executable = _executable()
    if not executable:
        raise FileNotFoundError('Установите Codex CLI и добавьте исполняемый файл в PATH.')
    arguments = [executable, 'exec', '--cd', str(Path(root).resolve())]
    if resume:
        return [*arguments, 'resume', '--json', _thread_id(session), '-']
    if session is not None:
        raise ValueError('ID новой сессии назначает Codex в событии thread.started.')
    return [*arguments, '--json', '-']


def parse_event(event: dict) -> dict:
    kind = event.get('type')
    if kind == 'thread.started':
        return dict(session=_thread_id(event.get('thread_id')))
    if kind == 'item.completed':
        item = event.get('item')
        if isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
            return dict(text=item['text'])
    if kind == 'turn.completed':
        observation = dict(completed=True)
        usage = event.get('usage')
        if isinstance(usage, dict):
            fields = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
            known = {key: usage[key] for key in fields if type(usage.get(key)) is int and usage[key] >= 0}
            if known:
                observation['usage'] = known
        return observation
    if kind == 'turn.failed':
        reason = event.get('error')
        detail = reason.get('message') if isinstance(reason, dict) else reason
        return dict(failed=True, error=detail if isinstance(detail, str) else 'Ход Codex завершился ошибкой.')
    if kind == 'error' and isinstance(event.get('message'), str):
        # При переподключении Codex может продолжить текущий ход. Итог придёт отдельным событием.
        return dict(error=event['message'])
    return {}
