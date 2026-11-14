"""Вход в Landlock перед exec: дочерний CLI не может переписать cgroup.procs."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import platform
import sys
import time


def restrict_writes(paths):
    # Номера одинаковы в таблицах системных вызовов x86_64 и aarch64.
    if platform.machine() not in ('x86_64', 'aarch64'):
        raise OSError('Неподдерживаемая архитектура Linux Landlock.')
    libc = ctypes.CDLL(None, use_errno=True)
    def checked(code):
        if code < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return code

    # В ABI 3 появилась отдельная проверка truncate(2).
    version = checked(libc.syscall(444, 0, 0, 1))
    if version < 3:
        raise OSError('Для хода требуется Landlock ABI 3 или новее.')
    access = sum(1 << bit for bit in (1, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14))
    mask = ctypes.c_uint64(access)
    rules = checked(libc.syscall(444, ctypes.byref(mask), ctypes.sizeof(mask), 0))
    try:
        for raw in paths:
            path = Path(raw)
            # Только явно выделенные директории хода. Правила файловых систем
            # /proc и /sys не делегируются даже при ошибке подготовки окружения.
            if not path.is_absolute() or path != path.resolve() or path.anchor == str(path) or path.parts[1] in ('proc','sys','dev'):
                raise ValueError('Недопустимый каталог частного профиля хода.')
            # Открываем каждый компонент относительно уже закреплённого дескриптора.
            # Подмена символьной ссылки одновременно с проверкой не расширит правило.
            flags = os.O_PATH | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open('/', flags)
            try:
                for component in path.parts[1:]:
                    child = os.open(component, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                class Beneath(ctypes.Structure):
                    _pack_ = 1
                    _fields_ = [('access', ctypes.c_uint64), ('parent', ctypes.c_int32)]
                rule = Beneath(access, descriptor)
                checked(libc.syscall(445, rules, 1, ctypes.byref(rule), 0))
            finally:
                os.close(descriptor)
        checked(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        checked(libc.syscall(446, rules, 0))
    finally:
        os.close(rules)


def main():
    payload = Path(sys.argv[1])
    with payload.open(encoding='utf-8') as stream:
        data = json.load(stream)
    group = Path('/sys/fs/cgroup') / data['group'].lstrip('/') / 'cgroup.procs'
    if Path('/proc/self/cgroup').read_text().splitlines() != ['0::' + data['group']]:
        raise RuntimeError('Ядро не подтвердило принадлежность сервису хода.')
    restrict_writes(data['write_paths'])
    try:
        with group.open('wb', buffering=0):
            pass
    except PermissionError:
        deadline = time.monotonic() + 10
        while not (payload.parent / 'admitted').is_file():
            if time.monotonic() >= deadline or not payload.exists():
                raise RuntimeError('Владелец хода не подтвердил допуск к запуску.')
            time.sleep(.025)
        os.execvpe(data['command'][0], data['command'], data['env'])
    raise RuntimeError('Группа разрешила процессу перемещение. Запуск CLI остановлен.')


if __name__ == '__main__':
    main()
