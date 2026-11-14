"""Один ход в собственной cgroup v2, созданной менеджером systemd пользователя."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import UUID

CANCELLED = 125
CGROUP_ROOT = Path('/sys/fs/cgroup')
_WAIT = 10


def _unit(name):
    return 'chief-turn-' + UUID(name).hex + '.service'


def _ctl(*args):
    return subprocess.run(['systemctl', '--user', *args], env=dict(os.environ, LC_ALL='C'),
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=_WAIT, check=False, text=True)


def _state(unit):
    properties = 'LoadState,ActiveState,SubState,ControlGroup,ExecMainCode,ExecMainStatus,Result,InvocationID'
    result = _ctl('show', '--property=' + properties, '--', unit)
    state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if state.get('LoadState') == 'not-found' and state.get('ActiveState') == 'inactive':
        return None
    if result.returncode or state.get('LoadState') != 'loaded':
        raise RuntimeError('systemd не подтвердил состояние группы ' + unit)
    return state


def _group(unit):
    # Шаблон имени и slice фиксированы. Проверяем и группу, сохранившуюся без объекта unit.
    current = _ctl('show', '--property=ControlGroup', '--value', '--', 'app.slice')
    relative = current.stdout.strip()
    if current.returncode or not relative.startswith('/') or '..' in Path(relative).parts:
        raise RuntimeError('systemd не подтвердил путь к родительской группе app.slice.')
    return CGROUP_ROOT / relative.lstrip('/') / unit


def _populated(group):
    try:
        events = dict(line.split() for line in (group / 'cgroup.events').read_text(encoding='ascii').splitlines())
    except FileNotFoundError:
        if not group.exists():
            return False
        raise
    return events['populated'] == '1'


def _ended(unit, group):
    state = _state(unit)
    return (state is None or state['ActiveState'] in ('inactive', 'failed')) and not _populated(group)


def stop(name):
    unit = _unit(name)
    try:
        group = _group(unit)
        state = _state(unit)
        if state is not None:
            if _ctl('stop', '--', unit).returncode and not _ended(unit, group):
                return False
        deadline = time.monotonic() + _WAIT
        while not _ended(unit, group):
            if time.monotonic() >= deadline:
                return False
            time.sleep(.04)
        if state is not None and _state(unit) is not None:
            _ctl('reset-failed', '--', unit)
        return True
    except (KeyError, ValueError, OSError, subprocess.SubprocessError, RuntimeError):
        return False


def _runner_args(unit, cwd, io, executable, payload):
    properties = ('Type=exec', 'Slice=app.slice', 'RemainAfterExit=yes',
                  'KillMode=control-group', 'KillSignal=SIGKILL', 'TimeoutStopSec=5s',
                  'NoNewPrivileges=yes', 'Delegate=no', 'ProtectControlGroups=yes',
                  'ProtectKernelTunables=yes', 'RestrictNamespaces=yes')
    return ['systemd-run', '--user', '--quiet', '--unit=' + unit, '--working-directory=' + str(cwd),
            *('--property=' + prop for prop in properties),
            '--property=StandardInput=file:' + str(io[0]), '--property=StandardOutput=file:' + str(io[1]),
            '--property=StandardError=file:' + str(io[2]), '--', executable, '-I', '-S',
            str(Path(__file__).resolve().with_name('linux_turn_entry.py')), str(payload)]


def _copy(source, target):
    if source is None or source == subprocess.DEVNULL or target is None or target == subprocess.DEVNULL:
        return
    source_fd = source if isinstance(source, int) else source.fileno()
    target_fd = target if isinstance(target, int) else target.fileno()
    with os.fdopen(os.dup(source_fd), 'rb') as incoming, os.fdopen(os.dup(target_fd), 'wb') as outgoing:
        shutil.copyfileobj(incoming, outgoing)


def run(command, *, name, cwd, env, streams, should_stop, admission, write_dirs=()):
    if not (CGROUP_ROOT / 'cgroup.controllers').is_file():
        raise OSError('Для надзора за ходом нужен контроллер Linux cgroup v2.')
    unit = _unit(name)
    group = _group(unit)
    with tempfile.TemporaryDirectory(prefix='chief-turn-') as temp:
        root = Path(temp)
        payload = root / 'request.json'
        environment = dict(os.environ if env is None else env)
        workspace = Path(cwd).absolute()
        # Допускаем рабочую папку процесса и одноуровневые частные каталоги,
        # подготовленные native_worker. Ни домашний профиль пользователя, ни корень
        # репозитория не становятся записываемыми по наследованной переменной HOME.
        paths = {str(workspace), *(os.fspath(path) for path in write_dirs)}
        for variable in ('HOME', 'TMPDIR', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME',
                     'CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'APPDATA', 'LOCALAPPDATA'):
            path = Path(environment.get(variable, ''))
            if workspace.name == 'workspace' and workspace.parent.name.startswith('chief-worker-') and path.parent == workspace.parent:
                paths.add(str(path))
        payload.write_text(json.dumps(dict(command=[os.fspath(a) for a in command], env=environment,
                                          group='/' + group.relative_to(CGROUP_ROOT).as_posix(),
                                          write_paths=sorted(paths))), encoding='utf-8')
        io = [root / name for name in ('stdin', 'stdout', 'stderr')]
        for file in io:
            file.touch(mode=0o600)
        with io[0].open('wb') as incoming:
            _copy(streams[0], incoming)
        owned = False
        try:
            with admission():
                if should_stop():
                    return CANCELLED
                if _state(unit) is not None or _populated(group):
                    raise FileExistsError('Группа этого хода systemd уже существует.')
                arguments = _runner_args(unit, cwd, io, sys.executable, payload)
                owned = True
                result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, timeout=_WAIT,
                                        check=False, text=True)
                if result.returncode:
                    raise OSError('systemd отказал в создании изолированного хода: ' + result.stderr.strip())
                # Только владелец допуска разрешает exec. Поздний ответ D-Bus после
                # timeout или сбоя владельца не должен запустить исполнителя.
                (root / 'admitted').touch(exist_ok=False)
            while True:
                state = _state(unit)
                if state is None:
                    # RemainAfterExit удерживает естественный результат до его чтения.
                    # Объект исчезает после внешнего stop или перезапуска менеджера.
                    return CANCELLED
                if state['SubState'] == 'exited' or state['ActiveState'] in ('inactive', 'failed'):
                    status = int(state['ExecMainStatus'])
                    if state['ExecMainCode'] != '1' or state.get('Result') != 'success':
                        return status or 1
                    return status
                if should_stop():
                    return CANCELLED
                time.sleep(.08)
        finally:
            if owned and not stop(name):
                raise RuntimeError('systemd не подтвердил остановку всех процессов хода.')
            for source, stream in zip(io[1:], streams[1:]):
                with source.open('rb') as incoming:
                    _copy(incoming, stream)


def probe(name):
    if not all(shutil.which(binary) for binary in ('systemd-run', 'systemctl')):
        return False
    # Перед exec вход сервиса сам проверяет членство и невозможность открытия cgroup.procs на запись.
    from contextlib import nullcontext
    try:
        with tempfile.TemporaryDirectory(prefix='chief-probe-') as scratch:
            return run([sys.executable, '-I', '-S', '-c', 'pass'], name=name, cwd=scratch, env=None,
                       streams=(None, None, None), should_stop=lambda: False, admission=nullcontext) == 0
    except (KeyError, ValueError, OSError, subprocess.SubprocessError, RuntimeError):
        return False
