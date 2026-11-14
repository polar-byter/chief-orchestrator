"""Группа Windows Job Object с включением процесса в момент CreateProcessW."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

_kernel = c.WinDLL('kernel32', use_last_error=True)
PTR = c.c_void_p
SIZE = c.c_size_t
CANCELLED = 125


def _function(name, result, *args):
    entry = getattr(_kernel, name)
    entry.restype = result
    entry.argtypes = args
    return entry


class _Limits(c.Structure):
    _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                ('min_working_set', SIZE), ('max_working_set', SIZE), ('process_limit', w.DWORD),
                ('affinity', SIZE), ('priority', w.DWORD), ('scheduling', w.DWORD)]


class _Extended(c.Structure):
    _fields_ = [('basic', _Limits), ('io_counters', c.c_uint64 * 6),
                ('memory', SIZE * 4)]


class _Accounting(c.Structure):
    _fields_ = [('times', c.c_int64 * 4), ('faults', w.DWORD), ('total', w.DWORD),
                ('active', w.DWORD), ('terminated', w.DWORD)]


class _Startup(c.Structure):
    _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR), ('title', w.LPWSTR),
                ('geometry', w.DWORD * 7), ('flags', w.DWORD), ('show', w.WORD), ('reserved_size', w.WORD),
                ('reserved_data', PTR), ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]


class _StartupEx(c.Structure):
    _fields_ = [('info', _Startup), ('attributes', PTR)]


class _Process(c.Structure):
    _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]


_close = _function('CloseHandle', w.BOOL, w.HANDLE)
_create_job = _function('CreateJobObjectW', w.HANDLE, PTR, w.LPCWSTR)
_open_job = _function('OpenJobObjectW', w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
_set = _function('SetInformationJobObject', w.BOOL, w.HANDLE, c.c_int, PTR, w.DWORD)
_query = _function('QueryInformationJobObject', w.BOOL, w.HANDLE, c.c_int, PTR, w.DWORD, PTR)
_terminate = _function('TerminateJobObject', w.BOOL, w.HANDLE, w.UINT)
_self = _function('GetCurrentProcess', w.HANDLE)
_dup = _function('DuplicateHandle', w.BOOL, w.HANDLE, w.HANDLE, w.HANDLE, c.POINTER(w.HANDLE),
                 w.DWORD, w.BOOL, w.DWORD)
_init = _function('InitializeProcThreadAttributeList', w.BOOL, PTR, w.DWORD, w.DWORD, c.POINTER(SIZE))
_update = _function('UpdateProcThreadAttribute', w.BOOL, PTR, w.DWORD, SIZE, PTR, SIZE, PTR, PTR)
_free = _function('DeleteProcThreadAttributeList', None, PTR)
_create = _function('CreateProcessW', w.BOOL, w.LPCWSTR, w.LPWSTR, PTR, PTR, w.BOOL, w.DWORD,
                    PTR, w.LPCWSTR, c.POINTER(_StartupEx), c.POINTER(_Process))
_wait = _function('WaitForSingleObject', w.DWORD, w.HANDLE, w.DWORD)
_exit_code = _function('GetExitCodeProcess', w.BOOL, w.HANDLE, c.POINTER(w.DWORD))


def _check(ok):
    if not ok:
        raise c.WinError(c.get_last_error())


def _job_name(name):
    return 'Local\\ChiefTurn-' + name


@contextmanager
def _new_job(name):
    c.set_last_error(0)
    job = _create_job(None, _job_name(name))
    _check(job)
    try:
        if c.get_last_error() == 183:  # Коллизия UUID не даёт права на чужую группу.
            raise FileExistsError('Группа этого хода Windows уже существует.')
        limits = _Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; breakaway не разрешён.
        _check(_set(job, 9, c.byref(limits), c.sizeof(limits)))
        yield job
    finally:
        _close(job)


def _stream_handle(stream, reading, resources):
    if stream is None or stream == subprocess.DEVNULL:
        stream = resources.enter_context(open(os.devnull, 'rb' if reading else 'wb'))
    fileno = stream if isinstance(stream, int) else stream.fileno()
    handle = w.HANDLE()
    _check(_dup(_self(), msvcrt.get_osfhandle(fileno), _self(), c.byref(handle), 0, True, 2))
    resources.callback(_close, handle)
    return handle


def _spawn(command, *, cwd, env, streams, job, app_container=None):
    environment = os.environ if env is None else env
    executable = shutil.which(os.fspath(command[0]), path=environment.get('PATH'))
    if not executable:
        raise FileNotFoundError(f'Исполняемый файл не найден: {command[0]}')
    arguments = subprocess.list2cmdline([os.fspath(arg) for arg in command])
    for key, value in environment.items():
        if not key or '=' in key or '\0' in key or '\0' in value:
            raise ValueError('Недопустимая переменная окружения процесса.')
    block = '\0'.join(key + '=' + value for key, value in sorted(environment.items(), key=lambda pair: pair[0].upper())) + '\0\0'
    process = _Process()
    size = SIZE()
    count = 3 if app_container is not None else 2
    _init(None, count, 0, c.byref(size))
    if not size.value:
        raise c.WinError(c.get_last_error())
    attributes = c.create_string_buffer(size.value)
    with ExitStack() as resources:
        _check(_init(attributes, count, 0, c.byref(size)))
        resources.callback(_free, attributes)
        handles = (w.HANDLE * 3)(*[_stream_handle(stream, index == 0, resources)
                                   for index, stream in enumerate(streams)])
        jobs = (w.HANDLE * 1)(job)
        # Job list исключает окно между созданием процесса и его назначением в Job.
        # Handle list не даёт CLI унаследовать дескриптор самой группы или чужие каналы.
        _check(_update(attributes, 0, 0x2000D, jobs, c.sizeof(jobs), None, None))
        _check(_update(attributes, 0, 0x20002, handles, c.sizeof(handles), None, None))
        if app_container is not None:
            # Windows применяет профиль AppContainer и Job одним вызовом создания процесса.
            _check(_update(attributes, 0, 0x20009, c.byref(app_container), c.sizeof(app_container), None, None))
        startup = _StartupEx()
        startup.info.cb = c.sizeof(startup)
        startup.info.flags = 0x100  # STARTF_USESTDHANDLES
        startup.info.stdin, startup.info.stdout, startup.info.stderr = handles
        startup.attributes = c.cast(attributes, PTR)
        flags = subprocess.CREATE_NO_WINDOW | 0x80000 | 0x400  # EXTENDED_STARTUPINFO_PRESENT, CREATE_UNICODE_ENVIRONMENT
        _check(_create(executable, c.create_unicode_buffer(arguments), None, None, True, flags,
                       c.create_unicode_buffer(block), str(cwd), c.byref(startup), c.byref(process)))
    _close(process.thread)
    return process.process


def _empty(job):
    status = _Accounting()
    _check(_query(job, 1, c.byref(status), c.sizeof(status), None))
    return not status.active


def _drain(job):
    if _empty(job):
        return True
    _check(_terminate(job, CANCELLED))
    deadline = time.monotonic() + 8
    while not _empty(job):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.025)
    return True


def stop(name):
    try:
        job = _open_job(0x0C, False, _job_name(name))  # JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE
        if not job:
            return c.get_last_error() == 2
        try:
            return _drain(job)
        finally:
            _close(job)
    except OSError:
        return False


def run(command, *, name, cwd, env, streams, should_stop, admission, write_dirs=(), app_container=None):
    with ExitStack() as resources:
        with admission():
            if should_stop():
                return CANCELLED
            job = resources.enter_context(_new_job(name))
            process = _spawn(command, cwd=cwd, env=env, streams=streams, job=job, app_container=app_container)
            resources.callback(_close, process)
        try:
            while True:
                state = _wait(process, 80)
                if state == 0:
                    break
                if state != 258:
                    raise c.WinError(c.get_last_error())
                if should_stop():
                    return CANCELLED
            code = w.DWORD()
            _check(_exit_code(process, c.byref(code)))
            return code.value
        finally:
            if not _drain(job):
                raise RuntimeError('Windows не подтвердила выход всех процессов хода.')


def probe(name):
    try:
        return run([sys.executable, '-I', '-S', '-c', 'pass'], name=name, cwd=tempfile.gettempdir(), env=None,
                   streams=(None, None, None), should_stop=lambda: False, admission=nullcontext) == 0
    except (OSError, ValueError, RuntimeError):
        return False
