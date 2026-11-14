"""Привязка прямого хода из Obsidian к режиму проекта и группе процессов ОС."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .chief_mode import require_enabled
from .project_vault import publish_new
from .runtimes import native_process
from .task_control_state import operation_lock, plain_path


STOP_FILE = 'native-pause.json'


class UncertainProcessBoundary(RuntimeError):
    """Группа могла содержать живые процессы. Публиковать итог хода ещё рано."""


def pause_direct_project(root):
    """Вызывается после закрытия допуска к новым ходам под замком переключения режима."""
    directory = plain_path(Path(root) / '.metrics')
    if not directory.is_dir():
        return
    for path in directory.glob('*/documentation-sending.json'):
        attempt = json.loads(plain_path(path).read_bytes())
        if attempt.get('agent') not in ('claude', 'codex'):
            continue
        folder = path.parent
        if plain_path(folder / 'native-turn.json').is_file():
            continue
        boundary = attempt.get('process_boundary')
        if not boundary:
            raise ValueError('В журнале прежнего прямого запуска нет подтверждения завершения: ' + str(path))
        # При падении отправителя намерение остановить этот ход остаётся в журнале.
        pause = plain_path(folder / STOP_FILE)
        marker = json.dumps({'boundary': boundary}).encode()
        if not publish_new(pause, marker) and pause.read_bytes() != marker:
            raise ValueError('Остановка относится к другому прямому ходу: ' + str(pause))
        if not native_process.stop_and_confirm(boundary):
            raise ValueError('ОС не подтвердила остановку прямого хода: ' + str(path))


def _stop_legacy(process):
    if process.poll() is not None:
        return
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=8)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=8)


def _linux_profile(agent, env, temporary):
    if sys.platform != 'linux':
        return ()
    name = 'CODEX_HOME' if agent == 'codex' else 'CLAUDE_CONFIG_DIR'
    profile = plain_path(Path(env.get(name) or (Path.home() / ('.codex' if agent == 'codex' else '.claude'))))
    if not profile.is_absolute():
        raise ValueError('Для сохранения сессии нужен абсолютный каталог ' + name)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    env[name] = str(profile)
    env['TMPDIR'] = str(temporary)
    return (profile, temporary)


def run_direct(command, *, root, folder, attempt, stdin, stdout, stderr, temporary,
               timeout, claim, undo, read_events):
    """Регистрируем попытку до запуска. Доказанный отказ создания разрешает безопасный повтор."""
    deadline = time.monotonic() + timeout
    boundary = attempt.get('process_boundary')

    @contextmanager
    def admission():
        with operation_lock(root, 'chief-mode-switch'):
            require_enabled(root)
            claim()
            try:
                yield
            except FileNotFoundError:
                undo()
                raise

    def timed_out():
        read_events(False)
        return time.monotonic() >= deadline

    if boundary:
        env = dict(os.environ)
        write_dirs = _linux_profile(attempt['agent'], env, temporary)
        try:
            code = native_process.run_owned(command, cwd=root, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                boundary=boundary, admission=admission, should_stop=timed_out, write_dirs=write_dirs)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if (plain_path(folder / 'documentation-sending.json').is_file()
                    and not native_process.stop_and_confirm(boundary)):
                raise UncertainProcessBoundary('ОС пока не подтвердила остановку прямого хода.') from None
            raise
    else:
        # Старая среда допускает один прямой ход. До его завершения переключение режима
        # получает занятый замок. После сбоя неопределённая попытка требует сверки.
        with admission():
            process = subprocess.Popen(command, cwd=root, stdin=stdin, stdout=stdout, stderr=stderr,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                                       start_new_session=os.name != 'nt')
            try:
                while process.poll() is None and not timed_out():
                    time.sleep(.08)
            finally:
                _stop_legacy(process)
            code = process.returncode
    read_events(True)
    if plain_path(folder / STOP_FILE).exists():
        return code, 'Прямой ход остановлен при выключении режима Чифа.'
    if time.monotonic() >= deadline:
        return code, f'CLI не завершил ход за {timeout} с.'
    return code, None
