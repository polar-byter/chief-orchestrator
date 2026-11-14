"""Доставка приложения и открытие карточки в отдельном профиле Obsidian."""

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
from urllib.request import urlopen
import uuid

from .task_control_state import plain_path


# Пакеты из одного выпуска издателя. Имя и контрольную сумму меняют вместе.
RELEASE = 'https://github.com/obsidianmd/obsidian-releases/releases/download/v1.15.1/'
PACKAGES = {
    'windows': ('Obsidian-1.15.1.exe', '218c7d30acc065355ce94719dd996d5e2a2347cde3811ea5dc29380583f02b58'),
    'x86_64': ('Obsidian-1.15.1.AppImage', '515ff49bb263d08841069adc46002dae94562e5041fb7762b3a1dcb7be10a51a'),
    'aarch64': ('Obsidian-1.15.1-arm64.AppImage', '2a0c0cae8329c708035c8aff905ba6bdec4153be300657cccb5a94ff0bbba898'),
}


def application_home() -> Path:
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / 'Chief' / 'Obsidian'
    if sys.platform == 'linux':
        return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'chief/obsidian'
    raise ValueError('Установка Obsidian поддерживается на Windows и Linux.')


def find_application() -> Path | None:
    candidates = []
    if sys.platform == 'win32':
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'obsidian\shell\open\command') as key:
                command = winreg.QueryValueEx(key, '')[0]
            match = re.match(r'^\s*(?:"([^"]+)"|(.+?\.exe))(?=\s|$)', command, re.I)
            if match:
                candidate = Path(match[1] or match[2])
                if candidate.is_absolute() and candidate.name.lower() == 'obsidian.exe':
                    candidates.append(candidate)
        except OSError:
            pass
        candidates += [Path(os.environ['LOCALAPPDATA']) / 'Programs/Obsidian/Obsidian.exe',
                       Path(os.environ['ProgramFiles']) / 'Obsidian/Obsidian.exe',
                       Path(os.environ['LOCALAPPDATA']) / 'Obsidian/Obsidian.exe']
    elif sys.platform == 'linux':
        candidates += [application_home() / 'Obsidian.AppImage', Path('/opt/Obsidian/obsidian'),
                       Path('/usr/bin/obsidian')]
    else:
        raise ValueError('Открытие базы поддерживается на Windows и Linux.')
    executable = shutil.which('Obsidian.exe' if sys.platform == 'win32' else 'obsidian')
    if executable:
        candidates.append(Path(executable))
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def download_package(name: str, digest: str, destination: Path) -> None:
    checksum = hashlib.sha256()
    with urlopen(RELEASE + name, timeout=60) as response, destination.open('xb') as output:
        for block in iter(lambda: response.read(1024 * 1024), b''):
            checksum.update(block)
            output.write(block)
    if checksum.hexdigest() != digest:
        raise ValueError(f'Контрольная сумма пакета {name} отличается от опубликованной.')


@contextmanager
def installation_lock(home: Path):
    """Два проекта могут одновременно обнаружить отсутствие приложения."""
    home.mkdir(parents=True, exist_ok=True)
    with plain_path(home / 'install.lock').open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError('Другой проект уже устанавливает Obsidian. Повторите открытие после установки.') from error
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError('Другой проект уже устанавливает Obsidian. Повторите открытие после установки.') from error
        yield


def install_application(home: Path) -> Path:
    architecture = 'windows' if sys.platform == 'win32' else platform.machine().lower()
    if architecture not in PACKAGES:
        raise ValueError(f'Для архитектуры {architecture} установочный пакет не предусмотрен.')
    name, digest = PACKAGES[architecture]
    print(f'Загрузка официального пакета {name}…', file=sys.stderr, flush=True)
    with tempfile.TemporaryDirectory(dir=home) as directory:
        package = Path(directory) / name
        download_package(name, digest, package)
        print('Контрольная сумма проверена. Установка Obsidian…', file=sys.stderr, flush=True)
        if sys.platform == 'win32':
            code = subprocess.run([str(package), '/currentuser', '/S'], check=False).returncode
            if code:
                raise ValueError(f'Установщик Obsidian завершился с кодом {code}. Повторите открытие базы.')
        else:
            package.chmod(0o755)
            package.replace(home / 'Obsidian.AppImage')
    executable = find_application()
    if executable is None:
        raise ValueError('После установки не найден исполняемый файл Obsidian.')
    return executable


def ensure_application() -> Path:
    executable = find_application()
    if executable:
        return executable
    home = application_home()
    with installation_lock(home):
        return find_application() or install_application(home)


def prepare_profile(vault: Path, home: Path) -> Path:
    # Отдельный каталог на машину и путь: перенос базы создаёт новый локальный
    # индекс. Внутри самой базы роли, этапы, фичи и рукописные заметки сохраняются.
    identifier = hashlib.sha256(os.fsencode(os.path.normcase(str(vault)))).hexdigest()[:16]
    profile = plain_path(home / identifier)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry = plain_path(profile / 'obsidian.json')
    if not registry.exists():
        # Другой процесс может одновременно впервые подключать эту же базу.
        # Публикуем целый файл. Проигравший гонку читает уже созданный реестр.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=profile, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'vaults': {identifier: {'path': str(vault), 'ts': int(time.time() * 1000),
                                               'open': True}}}, stream, ensure_ascii=False)
        try:
            try:
                if os.name == 'nt':
                    temporary.rename(registry)
                else:
                    os.link(temporary, registry)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    registry_data = json.loads(registry.read_text(encoding='utf-8'))
    if not isinstance(registry_data, dict) or not isinstance(registry_data.get('vaults'), dict):
        raise ValueError(f'Obsidian не смог прочитать реестр хранилищ: {registry}')
    registered = registry_data['vaults'].get(identifier) or {}
    if not isinstance(registered, dict) or not isinstance(registered.get('path'), str) or Path(registered['path']) != vault:
        raise ValueError(f'Профиль Obsidian принадлежит другому хранилищу: {profile}')
    return profile


def active_note(workspace: Path) -> tuple[str, int] | None:
    """Берёт файл активной вкладки редактора. Боковые панели тоже содержат поле file."""
    try:
        with workspace.open(encoding='utf-8') as stream:
            stamp = os.fstat(stream.fileno()).st_mtime_ns
            state = json.load(stream)
        nodes = [state['main']]
        while nodes:
            node = nodes.pop()
            if node.get('id') == state['active']:
                view = node.get('state', {})
                if view.get('type') == 'markdown':
                    return view['state']['file'], stamp
            nodes.extend(node.get('children', []))
    except (FileNotFoundError, AttributeError, KeyError, TypeError, json.JSONDecodeError):
        # Во время сохранения Obsidian может ещё не опубликовать новую раскладку.
        pass
    return None


@contextmanager
def windows_reply(uri: str):
    """Канал ответа привязывает второй запуск к экземпляру с тем же профилем.

    Обычная команда Obsidian.exe переадресуется в общую трубу пользователя.
    Служебный аргумент session использует тот же механизм, что и Obsidian.com.
    """
    from multiprocessing.connection import Client, Listener

    name = 'chief-' + uuid.uuid4().hex
    address = '\\\\.\\pipe\\' + name
    received = threading.Event()
    with Listener(address, family='AF_PIPE') as listener:
        def read_reply():
            try:
                with listener.accept() as connection:
                    if connection.poll(5) and connection.recv_bytes(65536).decode().strip() == 'Processed URI ' + uri:
                        received.set()
            except (EOFError, OSError, UnicodeError):
                pass

        reader = threading.Thread(target=read_reply, daemon=True)
        reader.start()
        try:
            yield ['session=' + name], received
        finally:
            # При первом запуске Obsidian сам открывает URI без обратной трубы.
            # Подключение освобождает поток, всё ещё ожидающий вызова accept.
            if reader.is_alive():
                try:
                    with Client(address, family='AF_PIPE'):
                        pass
                except OSError:
                    pass
            reader.join(timeout=1)


def wait_for_note(executable: Path, profile: Path, note: Path, vault: Path,
                  reply_arguments: list[str], received: threading.Event | None) -> None:
    uri = 'obsidian://open?path=' + quote(str(note), safe='')
    workspace = plain_path(vault / '.obsidian/workspace.json')
    previous = active_note(workspace)
    relative = note.relative_to(vault).as_posix()
    command = [str(executable), '--user-data-dir=' + str(profile),
               '--user-data-directory=' + str(profile), *reply_arguments, uri]
    env = os.environ.copy()
    if sys.platform == 'linux':
        # Obsidian обнаруживает свою CLI-службу через XDG_RUNTIME_DIR. Для
        # Wayland сохраняем путь к уже работающему дисплею рабочего стола.
        runtime = Path(env.get('XDG_RUNTIME_DIR') or tempfile.gettempdir())
        display = env.get('WAYLAND_DISPLAY')
        if display and not Path(display).is_absolute():
            env['WAYLAND_DISPLAY'] = str(runtime / display)
        socket_dir = runtime / f'chief-{os.getuid()}-{profile.name}'
        plain_path(socket_dir).mkdir(mode=0o700, exist_ok=True)
        if socket_dir.stat().st_uid != os.getuid() or socket_dir.stat().st_mode & 0o077:
            raise ValueError(f'Каталог связи Obsidian должен быть доступен только владельцу: {socket_dir}')
        env['XDG_RUNTIME_DIR'] = str(socket_dir)
        if executable.suffix == '.AppImage':
            from ctypes.util import find_library
            if not find_library('fuse'):
                env['APPIMAGE_EXTRACT_AND_RUN'] = '1'
    log = tempfile.TemporaryFile() if sys.platform == 'linux' else nullcontext(subprocess.DEVNULL)
    with log as output:
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=sys.platform != 'win32')
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            code = process.poll()
            if code not in (None, 0):
                raise ValueError(f'Obsidian не открыл хранилище {vault} (код {code}).')
            current = active_note(workspace)
            if current and current[0] == relative:
                if current != previous or (received and received.is_set()):
                    return
                if sys.platform == 'linux' and code == 0:
                    output.seek(0)
                    if ('Processed URI ' + uri).encode() in output.read():
                        return
            time.sleep(0.15)
    raise ValueError(f'Obsidian не подтвердил открытие «{relative}» в базе {vault}. Проверьте окно приложения.')


def open_note(vault: Path, note: Path, *, profile_home: Path | None = None) -> Path:
    """Находит или ставит приложение и возвращает подтверждённую активную заметку."""
    vault = plain_path(Path(vault).absolute())
    note = plain_path(vault / note)
    if not note.is_relative_to(vault) or note.suffix.lower() != '.md' or not note.is_file():
        raise ValueError('Для открытия нужна существующая заметка Markdown внутри выбранной базы.')
    if sys.platform == 'linux' and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        raise ValueError('Для открытия Obsidian необходим графический сеанс Linux.')
    executable = ensure_application()
    home = Path(profile_home) if profile_home is not None else application_home() / 'profiles'
    profile = prepare_profile(vault, home.absolute())
    if sys.platform == 'win32':
        uri = 'obsidian://open?path=' + quote(str(note), safe='')
        with windows_reply(uri) as (arguments, received):
            wait_for_note(executable, profile, note, vault, arguments, received)
    else:
        wait_for_note(executable, profile, note, vault, [], None)
    return note
