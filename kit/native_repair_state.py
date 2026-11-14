"""Хранение цепочки исправлений и проверок кандидата в транзакции очереди."""
import json
import math
import re
import time

from .execution_policy import budget_reason, normalize_policy


TOKEN_COUNTERS = ('input_tokens', 'output_tokens', 'cached_input_tokens',
                  'reasoning_output_tokens', 'cache_creation_input_tokens')


def available(db):
    """Прежние журналы читаются без создания новых таблиц."""
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repair_attempts'").fetchone() is not None


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS repair_chains (
        root_id TEXT PRIMARY KEY, original_hash TEXT NOT NULL, policy TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS repair_attempts (
        job_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, previous_job TEXT UNIQUE, number INTEGER NOT NULL,
        executor_failures_before INTEGER NOT NULL, fixer_failures_before INTEGER NOT NULL,
        status TEXT NOT NULL, last_check TEXT, usage TEXT, usage_scope TEXT, handoff_closed INTEGER NOT NULL DEFAULT 0,
        UNIQUE(root_id, number))''')
    db.execute('''CREATE TABLE IF NOT EXISTS candidate_checks (
        boundary TEXT PRIMARY KEY, job_id TEXT NOT NULL, generation INTEGER NOT NULL, admitted INTEGER NOT NULL,
        started_at REAL NOT NULL, finished_at REAL, evidence TEXT)''')


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _record(row):
    policy = json.loads(row['policy'])
    # Расход предков зафиксирован в момент передачи. Поздний повтор проверки
    # либо инфраструктурное прерывание не меняет бюджет предыдущих исполнителей.
    failed = row['status'] == 'failed'
    executor = row['executor_failures_before'] + int(failed and row['number'] == 1)
    fixer = row['fixer_failures_before'] + int(failed and row['number'] > 1)
    evidence = json.loads(row['evidence']) if row['evidence'] is not None else None
    return dict(root_id=row['root_id'], previous_job=row['previous_job'], number=row['number'],
                status=row['status'], original_hash=row['original_hash'], policy=policy,
                used_executor=executor, used_fixer=fixer,
                remaining_total=policy['maxAttempts'] - executor - fixer,
                remaining_fixer=policy['fixerMaxAttempts'] - fixer, check=evidence,
                started_at=row['started_at'], finished_at=row['finished_at'], handoff_closed=bool(row['handoff_closed']),
                usage=json.loads(row['usage']) if row['usage'] is not None else None,
                usage_scope=row['usage_scope'])


def records(db, job_id=None):
    if not available(db):
        return {}
    sql = '''SELECT a.*, c.original_hash, c.policy, t.started_at, t.finished_at, t.evidence
             FROM repair_attempts a JOIN repair_chains c USING(root_id)
             LEFT JOIN candidate_checks t ON t.boundary=a.last_check'''
    params = ()
    if job_id is not None:
        sql += ' WHERE a.job_id=?'
        params = (job_id,)
    return {row['job_id']: _record(row) for row in db.execute(sql, params)}


def add_attempt(db, job, policy, previous_job):
    ensure_schema(db)
    root_id = job['id']
    policy = normalize_policy(policy)
    executor = fixer = 0
    number = 1
    if previous_job is None:
        if job['role'] != 'executor':
            raise ValueError('Первое поручение с проверкой выдаётся исполнителю.')
        db.execute('INSERT INTO repair_chains VALUES (?,?,?)', (root_id, job['baseline_hash'], _encode(policy)))
    else:
        previous = records(db, previous_job).get(previous_job)
        if (previous is None or previous['status'] != 'failed' or previous['handoff_closed']
                or job['role'] != 'bug_fixer'):
            raise ValueError('Исправителю нужен подтверждённый провал теста предыдущего кандидата.')
        source = db.execute('SELECT * FROM jobs WHERE id=?', (previous_job,)).fetchone()
        if (job['baseline_hash'] != source['candidate_hash'] or job['assigned_file'] != source['assigned_file']
                or job['instructions'] != source['instructions'] or job['contracts'] != json.loads(source['contracts'])
                or policy != previous['policy']):
            raise ValueError('Исходник исправителя, требования, контракты и бюджет должны совпадать с передачей.')
        if db.execute('SELECT 1 FROM repair_attempts WHERE previous_job=?', (previous_job,)).fetchone():
            raise ValueError('Для этого провала уже назначен исправитель.')
        executor, fixer = previous['used_executor'], previous['used_fixer']
        if budget_reason(dict(executor=executor, bug_fixer=fixer), policy, 'bug_fixer'):
            raise ValueError('Бюджет исправления исчерпан.')
        root_id, number = previous['root_id'], previous['number'] + 1
    db.execute('''INSERT INTO repair_attempts
        (job_id,root_id,previous_job,number,executor_failures_before,fixer_failures_before,status)
        VALUES (?,?,?,?,?,?,'pending')''', (job['id'], root_id, previous_job, number, executor, fixer))


def save_usage(db, job_id, result):
    if not available(db):
        return
    counters = result.get('usage')
    usage = {}
    if isinstance(counters, dict):
        usage = {key: counters[key] for key in TOKEN_COUNTERS
                 if type(counters.get(key)) is int and counters[key] >= 0}
    scope = result.get('usage_scope')
    if scope not in ('session_reported_total', 'cli_result'):
        scope = None
    db.execute('UPDATE repair_attempts SET usage=?, usage_scope=? WHERE job_id=?',
               (_encode(usage) if usage else None, scope, job_id))


def pending(db, *, handoff):
    if not available(db):
        return []
    if not handoff:
        rows = db.execute('''SELECT a.job_id FROM repair_attempts a JOIN jobs j ON j.id=a.job_id
                             WHERE a.status='pending' AND j.status='completed' ORDER BY j.rowid''')
        return [r[0] for r in rows]
    reviews = records(db)
    parents = {review['previous_job'] for review in reviews.values()}
    return [identifier for identifier, review in reviews.items()
            if review['status'] == 'failed' and identifier not in parents and not review['handoff_closed']
            and min(review['remaining_total'], review['remaining_fixer']) > 0]


def clear_handoffs(db):
    """Снятие очереди касается также подтверждённого отказа, по которому диспетчер ещё не записал потомка."""
    if available(db):
        db.execute("""UPDATE repair_attempts SET handoff_closed=1 WHERE status='failed' AND NOT EXISTS
                      (SELECT 1 FROM repair_attempts child WHERE child.previous_job=repair_attempts.job_id)""")


def active(db):
    if not available(db):
        return []
    return [r[0] for r in db.execute("SELECT last_check FROM repair_attempts WHERE status='checking'")]


def begin(db, job_id, boundary, generation):
    if not available(db):
        return False
    changed = db.execute("""UPDATE repair_attempts SET status='checking',last_check=? WHERE job_id=? AND status='pending'
        AND EXISTS (SELECT 1 FROM jobs WHERE id=? AND status='completed')""", (boundary, job_id, job_id))
    if changed.rowcount != 1:
        return False
    db.execute('INSERT INTO candidate_checks(boundary,job_id,generation,admitted,started_at) VALUES (?,?,?,0,?)',
               (boundary, job_id, generation, time.time()))
    return True


def checking(db, job_id, boundary):
    if not available(db):
        return False
    return db.execute("""SELECT 1 FROM candidate_checks t JOIN repair_attempts a ON a.last_check=t.boundary
        JOIN team ON team.singleton=1 WHERE t.boundary=? AND a.job_id=? AND team.status='running'
        AND team.generation=t.generation AND a.status='checking'""", (boundary, job_id)).fetchone() is not None


def admit(db, job_id, boundary):
    if not checking(db, job_id, boundary) or db.execute(
            'UPDATE candidate_checks SET admitted=1 WHERE boundary=? AND admitted=0', (boundary,)).rowcount != 1:
        raise ValueError('Право запуска проверки отозвано или уже использовано.')


def evidence_for(candidate_hash, evidence):
    if not isinstance(evidence, dict) or evidence.get('verdict') not in ('passed', 'failed', 'blocked'):
        raise ValueError('Проверяющий процесс должен вернуть исход проверки.')
    # В отчёт попадают только публичные результаты тестов, без исходника и вывода модели.
    verdict = evidence['verdict']
    result = dict(verdict=verdict, candidate_hash=candidate_hash)
    for field in ('tests', 'failures', 'errors', 'skipped'):
        value = evidence.get(field, 0 if verdict == 'blocked' else None)
        if type(value) is not int or value < 0:
            raise ValueError('Нужны целые неотрицательные счётчики проверки.')
        result[field] = value
    if verdict != 'blocked' and (evidence.get('candidate_hash') != candidate_hash or result['tests'] < 1
            or result['failures'] > result['tests'] or result['errors'] or result['skipped']
            or (verdict == 'failed') != (result['failures'] > 0)):
        raise ValueError('Счётчики теста или отпечаток проверенного кандидата расходятся с исходом.')
    for key, limit in (('diagnostic', 2000), ('test_name', 500)):
        if isinstance(evidence.get(key), str):
            result[key] = evidence[key][:limit]
    duration = evidence.get('duration_seconds')
    if type(duration) in (float, int) and math.isfinite(duration) and duration >= 0:
        result['duration_seconds'] = duration
    hashes = evidence.get('test_hashes')
    if isinstance(hashes, dict) and len(hashes) <= 32 and all(
            isinstance(k, str) and len(k) <= 500 and isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
            for k, v in hashes.items()):
        result['test_hashes'] = hashes
    return result


def finish(db, job_id, boundary, evidence):
    if not checking(db, job_id, boundary):
        return False
    if evidence['verdict'] != 'blocked' and not db.execute(
            'SELECT admitted FROM candidate_checks WHERE boundary=?', (boundary,)).fetchone()[0]:
        raise ValueError('Тест ещё не получил допуск к выполнению.')
    db.execute('UPDATE candidate_checks SET finished_at=?,evidence=? WHERE boundary=?',
               (time.time(), _encode(evidence), boundary))
    db.execute('UPDATE repair_attempts SET status=? WHERE job_id=?', (evidence['verdict'], job_id))
    return True


def interrupt(db, *, cancel):
    if not available(db):
        return
    for row in db.execute("""SELECT a.job_id, a.last_check, j.candidate_hash FROM repair_attempts a
                             JOIN jobs j ON j.id=a.job_id WHERE a.status='checking'""").fetchall():
        evidence = evidence_for(row['candidate_hash'], dict(verdict='blocked', diagnostic='Проверка остановлена командой.'))
        db.execute('UPDATE candidate_checks SET finished_at=?, evidence=? WHERE boundary=?',
                   (time.time(), _encode(evidence), row['last_check']))
        db.execute('UPDATE repair_attempts SET status=? WHERE job_id=?',
                   ('discarded' if cancel else 'interrupted', row['job_id']))
        db.execute('UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?',
                   ('cancelled' if cancel else 'interrupted', 'Проверка прервана', time.time(), row['job_id']))
    if cancel:
        db.execute("UPDATE repair_attempts SET status='discarded' WHERE status='interrupted'")


def retry_check(db, job_id, *, interrupted=False):
    status = 'interrupted' if interrupted else 'blocked'
    result = db.execute("""UPDATE repair_attempts SET status='pending', last_check=NULL
                           WHERE job_id=? AND status=?""", (job_id, status))
    if result.rowcount != 1:
        raise ValueError('Повтор можно назначить только для прерванной или заблокированной проверки.')

