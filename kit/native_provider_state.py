"""Свидетельства отказа API принадлежат попытке, включая её частичный файл для ручной сверки."""
from hashlib import sha256
import re


# Исходник до 1 МиБ после восстановления CRLF и UTF-8 BOM может занять почти вдвое больше.
MAX_PARTIAL_BYTES = 2 * 1024 * 1024 + 3
MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}')


def _available(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_incidents'").fetchone() is not None


def _model_id(value):
    """Принимаем идентификатор из настроек запуска. Адреса, ключи и тексты ошибок в журнал не входят."""
    if (not isinstance(value, str) or not MODEL_ID.fullmatch(value) or '://' in value
            or value.lower().startswith(('sk-', 'bearer:', 'oauth:', 'github_pat_', 'ghp_'))):
        return None
    return value


def save(db, job, reason, partial, model):
    if partial is not None:
        if not job['assigned_file'] or not isinstance(partial, bytes) or len(partial) > MAX_PARTIAL_BYTES or b'\0' in partial:
            raise ValueError('Для сверки нужен текст назначенного файла в пределах размера поручения.')
        partial.decode('utf-8')
    digest = sha256(partial).hexdigest() if partial is not None else None
    model = _model_id(model)
    db.execute('''CREATE TABLE IF NOT EXISTS provider_incidents (
        job_id TEXT NOT NULL, attempt INTEGER NOT NULL, boundary TEXT UNIQUE NOT NULL,
        reason TEXT NOT NULL, model TEXT, partial BLOB, partial_sha256 TEXT, PRIMARY KEY(job_id, attempt))''')
    saved = db.execute('''SELECT reason,model,partial_sha256 FROM provider_incidents
                          WHERE job_id=? AND attempt=?''', (job['id'], job['attempt'])).fetchone()
    if saved is None:
        db.execute('INSERT INTO provider_incidents VALUES (?,?,?,?,?,?,?)',
                   (job['id'], job['attempt'], job['boundary'], reason, model, partial, digest))
    elif tuple(saved) != (reason, model, digest):
        raise ValueError('Для этой попытки уже сохранено другое свидетельство отказа.')


def attach(db, jobs):
    if not jobs or not _available(db):
        return
    by_id = {job['id']: job for job in jobs}
    # SQLite читает длину BLOB из заголовка. При опросе дашборда сам файл не загружается.
    select = '''SELECT job_id, attempt, reason, model, length(partial) AS partial_size, partial_sha256
                FROM provider_incidents'''
    if len(jobs) == 1:
        rows = db.execute(select + ' WHERE job_id=? ORDER BY attempt', (jobs[0]['id'],))
    else:
        rows = db.execute(select + ' ORDER BY job_id, attempt')
    for row in rows:
        job = by_id.get(row['job_id'])
        if job is not None:
            incident = dict(row)
            del incident['job_id']
            job.setdefault('provider_incidents', []).append(incident)


def read_partial(db, job_id, attempt):
    if type(attempt) is not int or attempt < 1:
        raise ValueError('Укажите положительный номер попытки.')
    if _available(db):
        row = db.execute('''SELECT partial, partial_sha256 FROM provider_incidents
                            WHERE job_id=? AND attempt=?''', (job_id, attempt)).fetchone()
        if row is not None and row['partial'] is not None:
            if sha256(row['partial']).hexdigest() != row['partial_sha256']:
                raise ValueError('Контрольная сумма частичного файла изменилась.')
            return row['partial']
    raise ValueError('У этой попытки нет сохранённого частичного файла.')
