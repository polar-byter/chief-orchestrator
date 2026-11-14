"""Владение процессами одного хода независимо от PID и дерева оболочки."""
from __future__ import annotations

from contextlib import nullcontext
import os
import re
import sys
from uuid import UUID, uuid4

if os.name == 'nt':
    from . import windows_job as backend
elif sys.platform == 'linux':
    from . import linux_turn_service as backend
else:
    backend = None


def _name(boundary: str) -> str:
    if not isinstance(boundary, str) or not re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}', boundary):
        raise ValueError('Имя группы процессов должно быть UUID хода.')
    return UUID(boundary).hex


def supported() -> bool:
    """Проверяет реальный запуск в ограниченной группе на данной машине."""
    return backend is not None and backend.probe(uuid4().hex)


def run_owned(command, *, cwd, env, stdin, stdout, stderr, boundary, should_stop, admission=nullcontext,
              write_dirs=()) -> int:
    """Допуск держится до помещения процесса в группу, но не во время работы модели."""
    name = _name(boundary)
    if not command or isinstance(command, (str, bytes)):
        raise ValueError('Передайте исполняемый файл и его аргументы списком.')
    if backend is None:
        raise OSError('Эта система не поддерживает подтверждение остановки всей команды.')
    return backend.run(command, name=name, cwd=cwd, env=env, streams=(stdin, stdout, stderr),
                       should_stop=should_stop, admission=admission, write_dirs=write_dirs)


def stop_and_confirm(boundary: str) -> bool:
    """Вызывается после закрытия допуска. Неизвестный результат оставляет паузу неподтверждённой."""
    name = _name(boundary)
    return backend is not None and backend.stop(name)
