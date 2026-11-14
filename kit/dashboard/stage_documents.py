"""Редактирование рабочих документов этапа и чтение зафиксированных источников."""
from codecs import BOM_UTF8
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from tempfile import NamedTemporaryFile
from urllib.parse import quote
from uuid import UUID

from ..project_revisions import _note_path
from ..project_workspace import SECTIONS, section_paths
from ..task_control_state import plain_path


MAX_TEXT_BYTES = 1024 * 1024
TITLES = {section.key: section.title for section in SECTIONS}


class DocumentConflict(ValueError):
    """Основа сохранения изменилась, пользователь должен сверить обе версии."""


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _table_exists(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _source(vault, stage, feature, section):
    if vault.record(stage)['kind'] != 'stage':
        raise ValueError('Выберите этап проекта.')
    record = vault.record(feature)
    if (record['parent'] or record['key']) != stage:
        raise ValueError('Документ принадлежит другому этапу.')
    if not isinstance(section, str) or section not in TITLES:
        raise ValueError('Выберите требования, BDD, NFR, трудозатраты или риски.')
    return section_paths(vault, feature)[section]


def _revision(db, key):
    if _table_exists(db, 'revision_history'):
        value = db.execute('SELECT MAX(number) FROM revision_history WHERE record_key=?', (key,)).fetchone()[0]
        if value is not None:
            return value
    # До появления истории карточка может иметь только рабочий текст.
    return 0


def _last_receipt(db, key, section):
    if not _table_exists(db, 'stage_document_edits'):
        return None
    row = db.execute("""SELECT receipt FROM stage_document_edits WHERE feature=? AND section=?
                        AND published=1 ORDER BY rowid DESC LIMIT 1""", (key, section)).fetchone()
    return json.loads(row[0]) if row else None


def read_document(vault, stage, feature, section, revision=None):
    """Открывает выбранную редакцию и возвращает точный отпечаток исходных байтов."""
    with vault.reading() as db:
        path = _source(vault, stage, feature, section)
        current = _revision(db, feature)
        receipt = None
        if revision is None:
            raw = path.read_bytes()
            text, digest, number = raw.decode('utf-8-sig'), _digest(raw), current
            receipt = _last_receipt(db, feature, section)
        else:
            if not re.fullmatch(r'[1-9][0-9]{0,8}', str(revision)):
                raise ValueError('Укажите положительный номер сохранённой редакции.')
            number = int(revision)
            row = db.execute('SELECT document FROM revision_history WHERE record_key=? AND number=?',
                             (feature, number)).fetchone() if current else None
            documents = json.loads(row[0]).get('documents', {}) if row else {}
            saved = documents.get(path.relative_to(vault.path).as_posix())
            if saved is None:
                raise FileNotFoundError('В выбранной редакции нет снимка этого раздела.')
            text, digest = saved['text'], saved['sha256']
            path = plain_path(vault.path / _note_path(feature, number))
    return dict(project_id=vault.project_id, stage=stage, feature=feature, section=section,
                title=TITLES[section], text=text, hash=digest, revision=number, readonly=revision is not None,
                obsidian_url='obsidian://open?path=' + quote(str(path), safe=''), last_receipt=receipt)


def _editor_bytes(text, original):
    # Поле textarea возвращает LF даже при исходном CRLF. Сохраняем принятый в заметке стиль и BOM.
    body = original.removeprefix(BOM_UTF8)
    keep_crlf = b'\r\n' in body and b'\n' not in body.replace(b'\r\n', b'')
    normalized = text.replace('\r\n', '\n')
    encoded = (normalized.replace('\n', '\r\n') if keep_crlf else normalized).encode('utf-8')
    return (BOM_UTF8 if original.startswith(BOM_UTF8) else b'') + encoded


def _replace_checked(path, new_bytes, expected_hash):
    """Публикует целую заметку. Внешнюю правку проверяет после подготовки временного файла."""
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.chief-edit-', delete=False) as stream:
            temporary = stream.name
            stream.write(new_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if _digest(plain_path(path).read_bytes()) != expected_hash:
            raise DocumentConflict('Рабочий текст изменился. Сверьте черновик с сохранённым документом.')
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _request(vault, stage, payload):
    if not isinstance(payload, dict):
        raise ValueError('Для сохранения нужны поля документа.')
    if payload.get('project_id') != vault.project_id:
        raise DocumentConflict('Привязка проекта изменилась. Откройте документ повторно.')
    if payload.get('stage', stage) != stage or payload.get('revision') is not None:
        raise ValueError('Сохранять можно только рабочий документ выбранного этапа.')
    feature, section = payload.get('feature'), payload.get('section')
    path = _source(vault, stage, feature, section)
    try:
        operation = str(UUID(str(payload.get('operation_id'))))
    except ValueError:
        raise ValueError('Для сохранения нужен UUID операции.') from None
    author, text = payload.get('author'), payload.get('text')
    if not isinstance(author, str) or not 1 <= len(author.strip()) <= 120:
        raise ValueError('Укажите автора сохранения (до 120 знаков).')
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_TEXT_BYTES or '\0' in text:
        raise ValueError('Допустим текст Markdown в UTF-8 объёмом до 1 МиБ.')
    digest, base = payload.get('expected_hash'), payload.get('expected_revision')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Не указан отпечаток прочитанного документа.')
    if type(base) is not int or base < 0:
        raise ValueError('Укажите номер исходной редакции.')
    receipt = dict(project_id=vault.project_id, stage=stage, feature=feature, section=section, author=author.strip(),
                   operation_id=operation, previous_hash=digest, revision=base)
    signature = _digest(json.dumps([receipt, text], sort_keys=True, ensure_ascii=False).encode('utf-8'))
    return path, text, receipt, signature


def _working_bytes(path):
    try:
        return plain_path(path).read_bytes()
    except FileNotFoundError as error:
        raise DocumentConflict('Документ удалён после открытия. Проверьте рабочее место в Obsidian.') from error


def save_document(vault, stage, payload):
    """Записывает рабочий текст. При успешном повторе возвращается та же квитанция."""
    path, text, receipt, signature = _request(vault, stage, payload)
    operation = receipt['operation_id']
    with vault.locked() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS stage_document_edits (
            operation_id TEXT PRIMARY KEY, feature TEXT NOT NULL, section TEXT NOT NULL,
            signature TEXT NOT NULL, receipt TEXT NOT NULL, published INTEGER NOT NULL)""")
        previous = db.execute('SELECT * FROM stage_document_edits WHERE operation_id=?', (operation,)).fetchone()
        if previous:
            if previous['signature'] != signature:
                raise DocumentConflict('Этот номер операции уже использован с другими параметрами.')
            receipt = json.loads(previous['receipt'])
            if previous['published']:
                return receipt
        else:
            if _revision(db, receipt['feature']) != receipt['revision']:
                raise DocumentConflict('Редакция карточки изменилась. Сверьте рабочий текст с новой редакцией.')
            current = _working_bytes(path)
            if _digest(current) != receipt['previous_hash']:
                raise DocumentConflict('Текст изменился после открытия. Сверьте изменения в рабочем документе.')
            receipt['hash'] = _digest(_editor_bytes(text, current))
            receipt['saved_at'] = datetime.now(timezone.utc).isoformat()
            unchanged = receipt['previous_hash'] == receipt['hash']
            # Намерение сохраняется до замены файла. После обрыва ответ можно восстановить по его хэшу.
            db.execute('INSERT INTO stage_document_edits VALUES (?,?,?,?,?,?)',
                       (operation, receipt['feature'], receipt['section'], signature,
                        json.dumps(receipt, ensure_ascii=False), int(unchanged)))
            if unchanged:
                return receipt

    with vault.locked() as db:
        published = db.execute('SELECT published FROM stage_document_edits WHERE operation_id=?',
                               (operation,)).fetchone()[0]
        if not published:
            raw = _working_bytes(path)
            actual_hash = _digest(raw)
            if actual_hash != receipt['hash']:
                if _revision(db, receipt['feature']) != receipt['revision'] or actual_hash != receipt['previous_hash']:
                    raise DocumentConflict('Основа документа изменилась. Сверьте черновик перед повторной записью.')
                _replace_checked(path, _editor_bytes(text, raw), actual_hash)
            db.execute('UPDATE stage_document_edits SET published=1 WHERE operation_id=?', (operation,))
    return receipt
