"""Профиль только для чтения и отдельный интерпретатор Windows для теста кандидата."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile


class SecurityCapabilities(ctypes.Structure):
    _fields_ = [('sid', ctypes.c_void_p), ('capabilities', ctypes.c_void_p),
                ('count', wintypes.DWORD), ('reserved', wintypes.DWORD)]


def copy_python(target):
    """Копируем стандартную библиотеку и DLL. Права исходной установки пользователя не меняются."""
    base = Path(sys.base_prefix)
    target.mkdir()
    executable = target / 'python.exe'
    shutil.copyfile(base / 'python.exe', executable)
    for library in base.glob('*.dll'):
        shutil.copyfile(library, target / library.name)
    shutil.copytree(base / 'DLLs', target / 'DLLs')
    standard = base / 'Lib'
    archive = target / f'python{sys.version_info.major}{sys.version_info.minor}.zip'
    with ZipFile(archive, 'w') as packed:
        for directory, children, files in os.walk(standard):
            children[:] = [name for name in children if name not in
                           ('__pycache__', 'site-packages', 'test', 'tests', 'ensurepip', 'idlelib')]
            for name in files:
                source = Path(directory) / name
                if source.suffix == '.py':
                    packed.write(source, source.relative_to(standard))
    return executable


@contextmanager
def read_only_container(directory, name):
    """У каждого запуска собственный SID без сетевых возможностей. Доступ выдаётся временной копии."""
    userenv = ctypes.WinDLL('userenv', use_last_error=True)
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    sid = ctypes.c_void_p()
    profile = 'Chief.Check.' + name
    create = userenv.CreateAppContainerProfile
    create.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
                       wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long
    status = create(profile, profile, profile, None, 0, ctypes.byref(sid))
    if status:
        raise OSError(f'Windows не создала профиль проверки (0x{status & 0xffffffff:08x}).')
    try:
        sid_text = ctypes.c_wchar_p()
        convert = advapi.ConvertSidToStringSidW
        convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        convert.restype = wintypes.BOOL
        if not convert(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            grant = '*' + sid_text.value + ':(OI)(CI)(RX)'
            # Изменяется только ACL временного дерева, принадлежность которого известна вызывающему.
            icacls = Path(os.environ['SystemRoot']) / 'System32/icacls.exe'
            result = subprocess.run([str(icacls), str(directory), '/grant', grant],
                                    capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise OSError('Windows не предоставила профилю чтение временного снимка.')
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        yield SecurityCapabilities(sid, None, 0, 0)
    finally:
        advapi.FreeSid.argtypes = [ctypes.c_void_p]
        advapi.FreeSid(sid)
        delete = userenv.DeleteAppContainerProfile
        delete.argtypes = [wintypes.LPCWSTR]
        delete.restype = ctypes.c_long
        delete(profile)
