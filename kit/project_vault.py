"""Карточки проекта в отдельной базе Obsidian и их связь с репозиторием."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4

from .journal import WINDOWS_RESERVED
from .task_control_state import operation_lock, plain_path


PROJECT_FILE = '.chief-project.json'
BINDING_FILE = '.metrics/project-vault.json'
DATABASE_FILE = '.chief/records.sqlite3'
HOME_NOTE = 'Документация проекта.md'


def _publish(path: Path, content: bytes, replace: bool) -> bool:
    path = plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.chief-', delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        elif os.name == 'nt':
            temporary.rename(path)
        else:
            os.link(temporary, path)
        return True
    except FileExistsError:
        if replace:
            raise
        return False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def publish_new(path: Path, content: bytes) -> bool:
    """Публикует целый файл. Если имя уже занято, сохраняет прежнее содержимое."""
    return _publish(path, content, replace=False)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _read_json(path: Path) -> dict:
    data = json.loads(plain_path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('format') != 1:
        raise ValueError(f'Нераспознанный формат привязки: {path}')
    return data


def _project_id(root: Path) -> str:
    value = _read_json(root / PROJECT_FILE).get('id')
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError(f'Некорректный ID проекта в {root / PROJECT_FILE}')
    return value


def _key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('ID записи: от 1 до 64 букв ASCII, цифр, дефисов и подчёркиваний.')
    if value.upper() in WINDOWS_RESERVED:
        raise ValueError(f'Зарезервированное имя: {value}')
    return value


def _title(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 for ch in value):
        raise ValueError('Укажите название в одну строку.')
    return value.strip()


def _filename(record) -> str:
    # В названии вкладки оставляем понятный пользователю текст. ID различает одноимённые фичи.
    stem = re.sub(r'[<>:"/\\|?*#^\[\]]+', ' ', record['title']).strip(' .')[:60].rstrip()
    return f"{stem or 'Карточка'} ({_key(record['key'])}).md"


def _card_path(record) -> Path:
    if record['kind'] == 'stage':
        return Path('Этапы', _filename(record))
    return Path('Возможности', _key(record['parent']), _filename(record))


def _work_path(record) -> Path:
    return Path('Рабочее место', _filename(record))


def _wiki(path: Path, label: str) -> str:
    return f'[[{path.with_suffix("").as_posix()}|{label}]]'


def _stage_list(folder: str) -> str:
    # Obsidian пересчитывает список сам. Добавление фичи не требует править заметку этапа.
    return f'\n```query\npath:"{folder}/"\n```\n'


def _database(path: Path, mode: str) -> sqlite3.Connection:
    database = plain_path(path / DATABASE_FILE)
    for suffix in ('-journal', '-wal', '-shm'):
        plain_path(database.with_name(database.name + suffix))
    connection = sqlite3.connect(database.as_uri() + '?mode=' + mode, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _verify_owner(connection: sqlite3.Connection, project_id: str) -> str:
    row = connection.execute('SELECT id, name FROM project WHERE singleton = 1').fetchone()
    if row is None or row['id'] != project_id:
        raise ValueError('Выбранная база принадлежит другому проекту.')
    return row['name']


def _binding_path(info: dict) -> Path:
    value = info.get('path')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('Сохранённый путь к базе должен быть абсолютным.')
    return plain_path(Path(value))


@dataclass(frozen=True)
class ProjectVault:
    root: Path
    path: Path
    project_id: str

    @property
    def home(self) -> Path:
        return plain_path(self.path / HOME_NOTE)

    @classmethod
    def connect(cls, root: Path, vault: Path | None = None, name: str | None = None) -> ProjectVault:
        root = plain_path(Path(root).absolute())
        if not root.is_dir() or root.parent == root:
            raise ValueError('Укажите существующий каталог проекта.')
        name = _title(root.name if name is None else name)
        with operation_lock(root, 'chief-project-vault'):
            identity = plain_path(root / PROJECT_FILE)
            binding = plain_path(root / BINDING_FILE)
            previous = _read_json(binding) if binding.exists() else {}
            if previous and not identity.exists():
                raise ValueError(f'Восстановите потерянный {PROJECT_FILE} с ID проекта.')
            if identity.exists():
                project_id = _project_id(root)
            else:
                project_id = str(uuid4())
            if previous and previous.get('id') != project_id:
                raise ValueError('Локальная привязка и репозиторий указывают на разные проекты.')
            old_path = _binding_path(previous) if previous else None
            default_path = root.parent / f'{root.name} — документация'
            path = plain_path(Path(vault or old_path or default_path).absolute())
            if root.is_relative_to(path):
                raise ValueError('Для базы нужен отдельный каталог, отличный от корня проекта и его родителей.')
            if old_path is not None and path != old_path and plain_path(old_path).exists():
                raise ValueError(f'Сначала перенесите исходную базу: {old_path}')
            database = plain_path(path / DATABASE_FILE)
            if previous and not database.is_file():
                raise ValueError('База не найдена. Укажите её новый путь для восстановления привязки.')
            if database.exists() and not identity.exists():
                raise ValueError(f'Для подключения существующей базы нужен исходный {PROJECT_FILE}.')
            if path.exists() and not database.exists() and any(path.iterdir()):
                # Только прямой повтор прерванного создания может встретить пустой служебный каталог.
                if set(path.iterdir()) != {database.parent} or not database.parent.is_dir() or any(database.parent.iterdir()):
                    raise ValueError('Укажите пустой каталог для новой базы или существующую базу этого проекта.')
            if not identity.exists():
                publish_new(identity, _json_bytes({'format': 1, 'id': project_id}))
                project_id = _project_id(root)
            instance = cls(root, path, project_id)
            instance._initialize(name)
            if not previous or old_path != path:
                _publish(binding, _json_bytes({'format': 1, 'id': project_id, 'path': str(path)}), replace=True)
            from .project_overview import refresh_overview
            refresh_overview(instance)
            return instance

    @classmethod
    def load(cls, root: Path) -> ProjectVault:
        root = plain_path(Path(root).absolute())
        info = _read_json(root / BINDING_FILE)
        path = _binding_path(info)
        if info.get('id') != _project_id(root):
            raise ValueError('Привязка базы не соответствует проекту.')
        instance = cls(root, plain_path(path), info['id'])
        with closing(instance._open('ro')):
            return instance

    def _initialize(self, name: str) -> None:
        plain_path(self.path / DATABASE_FILE).parent.mkdir(parents=True, exist_ok=True)
        with closing(_database(self.path, 'rwc')) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('CREATE TABLE IF NOT EXISTS project (singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                               'id TEXT NOT NULL, name TEXT NOT NULL)')
            connection.execute('INSERT OR IGNORE INTO project VALUES (1, ?, ?)', (self.project_id, name))
            name = _verify_owner(connection, self.project_id)
            connection.execute('CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY COLLATE NOCASE, '
                               'kind TEXT NOT NULL, title TEXT NOT NULL, parent TEXT, description TEXT NOT NULL)')
            home = f'# {name}\n\nОтсюда можно открыть этап и выбрать фичу для доработки.\n\n## Этапы\n'
            publish_new(self.home, (home + _stage_list('Этапы')).encode('utf-8'))

    def _open(self, mode: str) -> sqlite3.Connection:
        info = _read_json(self.root / BINDING_FILE)
        if info.get('id') != self.project_id or _binding_path(info) != self.path or _project_id(self.root) != self.project_id:
            raise ValueError('Привязка проекта изменилась. Откройте базу повторно.')
        if not plain_path(self.path / DATABASE_FILE).is_file():
            raise ValueError('База отсутствует по сохранённому пути. Укажите перенесённый каталог.')
        connection = _database(self.path, mode)
        try:
            _verify_owner(connection, self.project_id)
        except Exception:
            connection.close()
            raise
        return connection

    @contextmanager
    def reading(self):
        """Связанные представления читают одну версию каталога и редакций."""
        with closing(self._open('ro')) as connection, connection:
            connection.execute('BEGIN')
            yield connection

    @contextmanager
    def locked(self):
        """Сериализует изменение карточек и редакций. ОС снимет блокировку при сбое."""
        with closing(self._open('rw')) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            yield connection

    def record(self, key: str) -> dict:
        key = _key(key)
        with closing(self._open('ro')) as connection:
            row = connection.execute('SELECT * FROM records WHERE key=?', (key,)).fetchone()
        if row is None or row['key'] != key:
            raise ValueError(f'В этом проекте нет записи {key}.')
        return dict(row)

    def records(self) -> list[dict]:
        with closing(self._open('ro')) as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM records ORDER BY rowid')]

    def card(self, key: str) -> Path:
        return plain_path(self.path / _card_path(self.record(key)))

    def workspace(self, key: str) -> Path:
        return plain_path(self.path / _work_path(self.record(key)))

    def workspaces(self) -> dict[str, Path]:
        """Читает список одним запросом для обхода входящих заявок."""
        return {item['key']: self.path / _work_path(item) for item in self.records()}

    def add(self, kind: str, key: str, title: str, parent: str | None = None, description: str = '') -> Path:
        from .project_overview import refresh_overview
        from .project_revisions import ensure_history, workspace_template
        from .project_workspace import card_navigation, ensure_workspace

        key, title = _key(key), _title(title)
        if kind not in ('feature', 'stage') or (kind == 'feature') != (parent is not None):
            raise ValueError('Для фичи обязателен родительский этап. Этап создаётся без родителя.')
        item = dict(key=key, kind=kind, title=title, parent=parent, description=description)
        with self.locked() as connection:
            stage = self.record(parent) if parent is not None else None
            if stage and stage['kind'] != 'stage':
                raise ValueError('Родителем фичи должен быть этап.')
            existing = connection.execute('SELECT * FROM records WHERE key=?', (key,)).fetchone()
            if existing and dict(existing) != item:
                raise ValueError(f'ID {key} уже занят другой записью. Для изменения откройте рабочее место.')
            label = 'Доработать этап' if kind == 'stage' else 'Доработать фичу'
            card = plain_path(self.path / _card_path(item))
            work = plain_path(self.path / _work_path(item))
            card_text = (f'# {title}\n\n{description}' + card_navigation(key) + '\n' + _wiki(_work_path(item), label)
                         + f' · [[История/{key}/Редакции|История редакций]]\n'
                         + f'\n![[История/{key}/Редакции#Последнее решение]]\n')
            if stage:
                card_text += '\n' + _wiki(_card_path(stage), 'Открыть этап') + '\n'
            else:
                card_text += '\n## Фичи этапа\n' + _stage_list('Возможности/' + key)
            card_text += '\n' + _wiki(Path(HOME_NOTE), 'Открыть проект') + '\n'
            work_text = workspace_template(key, title, _card_path(item))
            # До первой фиксации в каталоге допускаем лишь собственные файлы
            # прерванной операции. После фиксации ручные правки остаются на месте.
            for file, content in ((card, card_text), (work, work_text)):
                encoded = content.encode('utf-8')
                if not publish_new(file, encoded) and not existing and file.read_bytes() != encoded:
                    raise ValueError(f'Сохранена существующая заметка: {file}. Выберите другой ID.')
            connection.execute('INSERT OR IGNORE INTO records (key, kind, title, parent, description) '
                               'VALUES (:key, :kind, :title, :parent, :description)', item)
        ensure_workspace(self, key)
        ensure_history(self, key)
        refresh_overview(self)
        return card
