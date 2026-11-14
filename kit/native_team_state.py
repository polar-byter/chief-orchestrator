"""Очередь одной команды. SQLite разделяет выдачу задания, запуск процесса и публикацию результата."""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from tempfile import NamedTemporaryFile
import time
from uuid import UUID, uuid4

from .delegation_guard import normalize_delegated_path
from . import native_provider_state as provider
from . import native_repair_state as repair
from .execution_policy import normalize_policy
from .journal import TaskJournal, WINDOWS_RESERVED
from .task_control_state import plain_path


DATABASE_NAME = 'native-team.sqlite3'
_ACTIVE = "('claimed','running','prepared')"
_SUMMARY = ('id, role, workspace, assigned_file, baseline_hash, candidate_hash, status, generation, '
            'boundary, attempt, error, result, created_at, updated_at')
_STOP_RANK = {'pause': 1, 'recovery': 1, 'api_error': 2, 'limit': 3, 'cancel': 4}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise ValueError('Нужен UUID операции.') from None


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    return _digest(plain_path(path).read_bytes())


def replace_source(path, baseline, candidate):
    """Повторяем сверку после подготовки файла. ОС публикует весь результат одной заменой."""
    path = plain_path(path)
    mode = stat.S_IMODE(path.stat().st_mode)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix='.chief-source-', delete=False) as stream:
            temp = Path(stream.name)
            stream.write(candidate)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        if _file_hash(path) != baseline:
            raise ValueError('Исходный файл изменился перед публикацией.')
        os.replace(temp, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


class NativeTeamState:
    def __init__(self, root: Path, task: str):
        self.root = plain_path(Path(root).absolute()).resolve()
        self.directory = TaskJournal(self.root).task_dir(task)
        self.task = task
        self.path = self.directory / DATABASE_NAME

    @contextmanager
    def _transaction(self, *, write=False, create=False):
        path = plain_path(self.path)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise ValueError('Для этой задачи ещё не создана нативная команда.')
        for suffix in ('-journal', '-wal', '-shm'):
            plain_path(path.with_name(path.name + suffix))
        mode = 'rwc' if create else 'rw' if write else 'ro'
        with closing(sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True, timeout=20)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            try:
                if not create:
                    self._team(db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _team(self, db):
        row = db.execute('SELECT * FROM team WHERE singleton=1').fetchone()
        if row is None or row['root'] != str(self.root) or row['task'] != self.task:
            raise ValueError('Журнал принадлежит другой задаче или другому каталогу проекта.')
        return row

    def _expect(self, db, generation, row=None, attempt=None):
        if generation is not None and self._team(db)['generation'] != generation:
            raise ValueError('Состояние команды изменилось. Обновите список поручений.')
        if row is not None and attempt is not None and row['attempt'] != attempt:
            raise ValueError('В этом поручении началась другая попытка. Обновите список.')

    def initialize(self, agent):
        if agent not in ('codex', 'claude'):
            raise ValueError('Выберите Codex или Claude Code.')
        with self._transaction(write=True, create=True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS team (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), root TEXT NOT NULL, task TEXT NOT NULL,
                agent TEXT NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT)''')
            db.execute("INSERT OR IGNORE INTO team VALUES (1,?,?,?,'running',1,NULL)", (str(self.root), self.task, agent))
            if self._team(db)['agent'] != agent:
                raise ValueError('У этой задачи уже есть команда другой среды.')
            db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, role TEXT NOT NULL, workspace TEXT NOT NULL, assigned_file TEXT, slot TEXT NOT NULL,
                instructions TEXT NOT NULL, contracts TEXT NOT NULL, signature TEXT NOT NULL, baseline_hash TEXT,
                status TEXT NOT NULL, generation INTEGER, boundary TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                candidate BLOB, candidate_hash TEXT, result TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
            db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS busy_slot ON jobs(slot) WHERE status IN {_ACTIVE}')
            db.execute('''CREATE TABLE IF NOT EXISTS previous_attempts (
                boundary TEXT PRIMARY KEY, id TEXT NOT NULL, attempt INTEGER NOT NULL, baseline_hash TEXT,
                candidate BLOB, candidate_hash TEXT, result TEXT, error TEXT, decision TEXT NOT NULL, ended_at REAL NOT NULL)''')
            return self._view(db)

    def _workspace(self, role, workspace):
        path = plain_path(self.root / Path(workspace)).resolve()
        if role == 'chief':
            if path != self.root:
                raise ValueError('Чиф должен быть привязан к корню проекта.')
        else:
            if path.parent != self.directory / 'workspaces' or path.name != _uuid(path.name):
                raise ValueError('Для исполнителя нужна отдельная копия данного задания в workspaces/<UUID>.')
        if not path.is_dir():
            raise ValueError('Рабочий каталог отсутствует.')
        return path

    @staticmethod
    def _source(workspace, name):
        if not isinstance(name, str) or not name:
            raise ValueError('Укажите заранее созданный исходный файл.')
        path = Path(name)
        if (path.anchor or '..' in path.parts or any(':' in part or part.endswith(('.', ' ')) or
                part.split('.')[0].upper() in WINDOWS_RESERVED for part in path.parts)):
            raise ValueError('Укажите обычный относительный путь внутри рабочей копии.')
        plain_path(workspace / path)
        return normalize_delegated_path(workspace, name)

    def enqueue(self, *, role, instructions, workspace, assigned_file=None, contracts=(), operation_id=None,
                verification=None, previous_job=None, expected_generation=None):
        identifier = _uuid(operation_id) if operation_id is not None else str(uuid4())
        if role not in ('chief', 'executor', 'bug_fixer') or not isinstance(instructions, str) or not instructions.strip():
            raise ValueError('Нужны роль участника и текст поручения.')
        if not isinstance(contracts, (list, tuple)) or any(not isinstance(name, str) for name in contracts):
            raise ValueError('Контракты задаются списком путей внутри рабочей копии.')
        directory = self._workspace(role, workspace)
        if role == 'chief' and assigned_file is not None:
            raise ValueError('Корневой ход принимает только текстовый ответ.')
        if role != 'chief':
            assigned_file = self._source(directory, assigned_file)
        contract_names = [self._source(directory, name) for name in contracts]
        if assigned_file and os.path.normcase(assigned_file) in {os.path.normcase(name) for name in contract_names}:
            raise ValueError('Назначенный файл не может одновременно быть неизменяемым контрактом.')
        identity = [role, str(directory), assigned_file, instructions, contract_names]
        if verification is not None:
            verification = normalize_policy(verification)
            previous_job = _uuid(previous_job) if previous_job is not None else None
            identity.extend((verification, previous_job))
        elif previous_job is not None:
            raise ValueError('Для передачи исправителю нужна исходная политика проверки.')
        signature = _digest(_json(identity).encode())
        with self._transaction(write=True) as db:
            existing = db.execute('SELECT signature FROM jobs WHERE id=?', (identifier,)).fetchone()
            if existing:
                if existing['signature'] != signature:
                    raise ValueError('UUID ранее использован для другого поручения.')
                job = self._job(db, identifier, details=True)
                self._attach_history(db, [job])
                return job
            self._expect(db, expected_generation)
            if self._team(db)['status'] != 'running':
                raise ValueError('Приём новых поручений закрыт.')
            now = time.time()
            baseline = _file_hash(directory / assigned_file) if assigned_file else None
            slot = 'file/' + os.path.normcase(assigned_file) if assigned_file else 'chief'
            db.execute('''INSERT INTO jobs (id,role,workspace,assigned_file,slot,instructions,contracts,signature,
                       baseline_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?)''',
                       (identifier, role, str(directory), assigned_file, slot, instructions, _json(contract_names),
                        signature, baseline, now, now))
            job = self._job(db, identifier, details=True)
            if verification is not None:
                repair.add_attempt(db, job, verification, previous_job)
                job['review'] = repair.records(db, identifier)[identifier]
            return job

    @staticmethod
    def _as_job(row, agent):
        result = dict(row, agent=agent)
        for field in ('contracts', 'result'):
            if result.get(field) is not None:
                result[field] = json.loads(result[field])
        return result

    def _job(self, db, identifier, *, details=False):
        columns = _SUMMARY + (',instructions,contracts' if details else '')
        row = db.execute(f'SELECT {columns} FROM jobs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Поручение этой команды не найдено.')
        return self._as_job(row, self._team(db)['agent'])

    def job(self, job_id):
        with self._transaction() as db:
            row = self._job(db, job_id, details=True)
            self._attach_history(db, [row])
            return row

    @staticmethod
    def _attach_history(db, jobs):
        provider.attach(db, jobs)
        reviews = repair.records(db, jobs[0]['id'] if len(jobs) == 1 else None)
        for row in jobs:
            if row['id'] in reviews:
                row['review'] = reviews[row['id']]
                # Снятое до теста поручение больше не ожидает запуска, в том числе в старых базах.
                if row['status'] == 'cancelled' and row['review']['status'] == 'pending':
                    row['review']['status'] = 'discarded'

    def _view(self, db):
        team = dict(self._team(db))
        jobs = [self._as_job(row, team['agent']) for row in db.execute(f'SELECT {_SUMMARY} FROM jobs ORDER BY rowid')]
        self._attach_history(db, jobs)
        active_jobs = [job['boundary'] for job in jobs if job['status'] in ('claimed', 'running', 'prepared')]
        return dict(task=self.task, agent=team['agent'], status=team['status'], generation=team['generation'],
                    reason=team['reason'], jobs=jobs,
                    active=active_jobs + repair.active(db))

    def view(self):
        with self._transaction() as db:
            return self._view(db)

    def operator_view(self, *, include_jobs=True):
        """Периодический опрос читает состояние очереди без текстов поручений и ответов модели."""
        with self._transaction() as db:
            return self._operator_view(db, include_jobs)

    def _operator_view(self, db, include_jobs):
        team = self._team(db)
        view = {key: team[key] for key in ('task', 'agent', 'status', 'generation', 'reason')}
        if include_jobs:
            rows = db.execute('''SELECT id, role, assigned_file, status, generation, attempt, boundary,
                              result IS NOT NULL AS prepared FROM jobs ORDER BY rowid''')
            view['jobs'] = [dict(row, prepared=bool(row['prepared'])) for row in rows]
            self._attach_history(db, view['jobs'])
        return view

    def review_info(self, job_id):
        with self._transaction() as db:
            return repair.records(db, job_id).get(job_id)

    def pending_checks(self):
        with self._transaction() as db:
            return repair.pending(db, handoff=False) if self._team(db)['status'] == 'running' else []

    def pending_handoffs(self):
        with self._transaction() as db:
            return repair.pending(db, handoff=True) if self._team(db)['status'] == 'running' else []

    def begin_check(self, job_id, boundary, expected_generation=None):
        boundary = _uuid(boundary)
        with self._transaction(write=True) as db:
            team = self._team(db)
            if team['status'] != 'running' or expected_generation not in (None, team['generation']):
                return False
            return repair.begin(db, job_id, boundary, team['generation'])

    @contextmanager
    def check_admission(self, job_id, boundary):
        with self._transaction(write=True) as db:
            repair.admit(db, job_id, boundary)
            yield

    def check_running(self, job_id, boundary):
        with self._transaction() as db:
            return repair.checking(db, job_id, boundary)

    def finish_check(self, job_id, boundary, evidence):
        with self._transaction(write=True) as db:
            if not repair.checking(db, job_id, boundary):
                return False
            row = self._job(db, job_id)
            result = repair.evidence_for(row['candidate_hash'], evidence)
            if result['verdict'] != 'blocked':
                try:
                    unchanged = _file_hash(self._target(row)) == row['candidate_hash']
                except (ValueError, OSError):
                    unchanged = False
                if not unchanged:
                    result.update(verdict='blocked', diagnostic='Не удалось подтвердить рабочую копию после проверки.')
            return repair.finish(db, job_id, boundary, result)

    def recheck(self, job_id, *, expected_generation=None, expected_attempt=None):
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            self._expect(db, expected_generation, row, expected_attempt)
            if self._team(db)['status'] != 'running' or row['status'] != 'completed' or not repair.available(db):
                raise ValueError('Для повторной проверки нужна завершённая попытка действующей команды.')
            if _file_hash(self._target(row)) != row['candidate_hash']:
                raise ValueError('Сначала восстановите проверяемый кандидат из этой попытки.')
            repair.retry_check(db, job_id)

    def final_review_view(self):
        """После проверки квитанций закрываем приём работ в той же транзакции, что и чтение отчёта."""
        with self._transaction(write=True) as db:
            view = self._operator_view(db, True)
            jobs = view['jobs']
            reviewed = [job for job in jobs if 'review' in job]
            has_provider_incident = any(job.get('provider_incidents') for job in jobs)
            if not reviewed and not has_provider_incident:
                return view
            parents = {job['review']['previous_job'] for job in reviewed}
            if any(job['status'] not in ('completed','cancelled','failed') for job in jobs):
                raise ValueError('Перед завершением задачи закройте работающие и ожидающие поручения.')
            if self._team(db)['status'] not in ('running', 'paused', 'finished'):
                raise ValueError('Нельзя принять остановленную или отменённую команду.')
            for job in reviewed:
                # Снятие исходного поручения без результата закрывает запись о нём.
                # У исправителя остаётся отклонённый предок: такую цепочку принять нельзя.
                if (has_provider_incident and job['status'] == 'cancelled' and not job['review']['previous_job']
                        and job['review']['status'] == 'discarded' and job['review']['check'] is None
                        and not job['prepared'] and (job['attempt'] == 0 or any(
                            fault['attempt'] == job['attempt'] for fault in job.get('provider_incidents', [])))):
                    continue
                if job['id'] in parents:
                    continue
                if job['status'] != 'completed' or job['review']['status'] != 'passed':
                    raise ValueError('Для каждой цепочки нужен успешно проверенный итоговый кандидат.')
                receipt = plain_path(self.directory / 'assignments' / (job['id'] + '.integrated.json'))
                if not receipt.is_file():
                    raise ValueError('Перед завершением перенесите проверенный результат в проект.')
                saved = json.loads(receipt.read_bytes())
                evidence = job['review']
                if (saved.get('job'), saved.get('path'), saved.get('previous_hash'), saved.get('hash')) != (
                        job['id'], job['assigned_file'], evidence['original_hash'], evidence['check']['candidate_hash']):
                    raise ValueError('Квитанция переноса относится к другой версии файла.')
                job['integrated'] = True
            if view['status'] != 'finished':
                db.execute("UPDATE team SET status='finished',reason=NULL,generation=generation+1")
                view.update(status='finished', reason=None, generation=view['generation'] + 1)
            return view

    @contextmanager
    def integration_job(self, job_id, *, expected_generation=None, expected_attempt=None):
        """Пауза ждёт окончания переноса, начатого оператором в текущем поколении."""
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            self._expect(db, expected_generation, row, expected_attempt)
            if self._team(db)['status'] == 'finished':
                raise ValueError('Отчёт команды зафиксирован, перенос файлов завершён.')
            if row['status'] != 'completed' or not row['assigned_file']:
                raise ValueError('Для переноса нужен завершённый инженерный ход.')
            self._attach_history(db, [row])
            if 'review' in row and row['review']['status'] != 'passed':
                raise ValueError('Перед переносом нужна успешная проверка именно этого кандидата.')
            yield row

    def claim(self, *, expected_generation=None):
        with self._transaction(write=True) as db:
            team = self._team(db)
            if team['status'] != 'running' or expected_generation not in (None, team['generation']):
                return None
            row = db.execute(f'''SELECT id FROM jobs AS waiting WHERE waiting.status='queued' AND NOT EXISTS (
                SELECT 1 FROM jobs AS busy WHERE busy.slot=waiting.slot AND busy.status IN {_ACTIVE})
                ORDER BY waiting.rowid LIMIT 1''').fetchone()
            if row is None:
                return None
            db.execute("""UPDATE jobs SET status='claimed', generation=?, boundary=?,
                       attempt=attempt+1, updated_at=? WHERE id=?""",
                       (team['generation'], str(uuid4()), time.time(), row['id']))
            return self._job(db, row['id'], details=True)

    def _admissible(self, db, row, boundary):
        team = self._team(db)
        return (row['boundary'] == boundary and row['generation'] == team['generation']
                and team['status'] == 'running' and row['status'] in ('claimed', 'running'))

    def is_running(self, job_id, boundary):
        with self._transaction() as db:
            return self._admissible(db, self._job(db, job_id), boundary)

    @contextmanager
    def admit(self, job_id, boundary):
        """Граница ОС и первый процесс создаются внутри этого контекста. Ожидание завершения — снаружи."""
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            if row['status'] != 'claimed' or not self._admissible(db, row, boundary):
                raise ValueError('Право запуска отозвано или уже использовано.')
            if row['assigned_file'] and _file_hash(self._target(row)) != row['baseline_hash']:
                raise ValueError('Исходный файл изменился между выдачей поручения и запуском.')
            db.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (time.time(), job_id))
            yield

    def _target(self, row):
        directory = self._workspace(row['role'], row['workspace'])
        source = self._source(directory, row['assigned_file'])
        return directory / source

    def checkpoint(self, job_id, boundary, *, candidate, result):
        if not isinstance(result, dict) or candidate is not None and not isinstance(candidate, bytes):
            raise ValueError('Укажите результат хода и точные байты кандидата.')
        encoded = _json(result)
        digest = _digest(candidate) if candidate is not None else None
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            if row['boundary'] != boundary:
                raise ValueError('Результат относится к другой попытке.')
            if row['status'] in ('prepared', 'completed'):
                if row['candidate_hash'] != digest or _json(row['result']) != encoded:
                    raise ValueError('Для этой попытки уже сохранён другой результат.')
                return row
            if row['status'] != 'running' or not self._admissible(db, row, boundary):
                raise ValueError('Право на сохранение результата отозвано.')
            if bool(row['assigned_file']) != (candidate is not None):
                raise ValueError('Для исполнителя нужен один файл, для Чифа нужен текстовый результат.')
            db.execute("""UPDATE jobs SET status='prepared', candidate=?, candidate_hash=?, result=?, updated_at=?
                       WHERE id=?""", (candidate, digest, encoded, time.time(), job_id))
            repair.save_usage(db, job_id, result)
            return self._job(db, job_id)

    def _publish(self, db, row):
        if row['assigned_file']:
            target = self._target(row)
            actual = _file_hash(target)
            if actual != row['candidate_hash']:
                if actual != row['baseline_hash']:
                    raise ValueError('Рабочий файл изменился. Сверьте его с сохранённой версией кандидата.')
                content = db.execute('SELECT candidate FROM jobs WHERE id=?', (row['id'],)).fetchone()[0]
                if content is None or _digest(content) != row['candidate_hash']:
                    raise ValueError('Не удалось подтвердить сохранённый результат.')
                replace_source(target, row['baseline_hash'], content)
        db.execute("UPDATE jobs SET status='completed', error=NULL, updated_at=? WHERE id=?", (time.time(), row['id']))
        return self._job(db, row['id'])

    def commit(self, job_id, boundary):
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            team = self._team(db)
            if row['boundary'] != boundary:
                raise ValueError('Результат относится к другой попытке.')
            if row['status'] == 'completed':
                return row
            if (row['status'] != 'prepared' or team['status'] != 'running'
                    or row['generation'] != team['generation']):
                raise ValueError('Публикация результата приостановлена.')
            return self._publish(db, row)

    def fail(self, job_id, boundary, reason):
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            if row['boundary'] != boundary:
                raise ValueError('Состояние другой попытки не может быть изменено.')
            # При гонке с остановкой разбирательство завершит оператор после проверки процессов.
            if self._admissible(db, row, boundary):
                db.execute("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                           (str(reason), time.time(), job_id))
            return self._job(db, job_id)

    def begin_stop(self, reason='pause', *, expected_generation=None):
        if reason not in _STOP_RANK:
            raise ValueError('Неизвестная причина остановки.')
        with self._transaction(write=True) as db:
            self._expect(db, expected_generation)
            return self._begin_stop(db, self._team(db), reason)

    def _begin_stop(self, db, team, reason):
        if team['status'] in ('cancelled', 'finished'):
            return self._view(db)
        if team['status'] != 'running' and _STOP_RANK[reason] <= _STOP_RANK[team['reason']]:
            return self._view(db)
        db.execute("UPDATE team SET status='stopping', generation=generation+1, reason=?", (reason,))
        if reason in ('limit', 'cancel'):
            self._clear(db)
        return self._view(db)

    def begin_provider_stop(self, job_id, boundary, reason, *, partial=None, model=None):
        """Отказ относится к конкретному допущенному ходу. Запись и запрет новых запусков неделимы."""
        if reason not in ('api_error', 'limit'):
            raise ValueError('Неизвестная причина отказа провайдера.')
        with self._transaction(write=True) as db:
            team = self._team(db)
            job = self._job(db, job_id)
            if not boundary or job['boundary'] != boundary or team['status'] in ('finished', 'cancelled'):
                return None
            if team['status'] == 'running':
                if job['generation'] != team['generation'] or job['status'] != 'running':
                    return None
            elif job['status'] not in ('running', 'interrupted'):
                return None
            # Другой участник мог первым запросить остановку. Пока решение по этому ходу
            # открыто, сохраняем его ответ и учитываем более строгую причину (например, квоту).
            provider.save(db, job, reason, partial, model)
            return self._begin_stop(db, team, reason)

    def provider_partial(self, job_id, attempt):
        """Чтение для оператора отдельно от публикации подготовленного результата."""
        with self._transaction() as db:
            return provider.read_partial(db, _uuid(job_id), attempt)

    def confirm_stop(self, generation, confirmed_boundaries):
        with self._transaction(write=True) as db:
            team = self._team(db)
            if generation != team['generation'] or team['status'] == 'running':
                raise ValueError('Получено устаревшее подтверждение остановки.')
            if team['status'] != 'stopping':
                return self._view(db)
            active = db.execute(f'SELECT {_SUMMARY} FROM jobs WHERE status IN {_ACTIVE}').fetchall()
            boundaries = {row['boundary'] for row in active} | set(repair.active(db))
            if boundaries != set(confirmed_boundaries):
                raise ValueError('Не подтверждена остановка каждой границы ОС этого поколения.')
            for row in active:
                # Если ОС заменила файл прежде, чем SQLite зафиксировала завершение, не записываем его ещё раз.
                already_written = False
                if (row['status'] == 'prepared' and row['assigned_file'] and row['candidate_hash'] != row['baseline_hash']):
                    try:
                        already_written = _file_hash(self._target(row)) == row['candidate_hash']
                    except (OSError, ValueError):
                        pass
                status = 'completed' if already_written else 'cancelled' if team['reason'] == 'cancel' else 'interrupted'
                db.execute('UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?',
                           (status, None if already_written else team['reason'], time.time(), row['id']))
            repair.interrupt(db, cancel=team['reason'] == 'cancel')
            if team['reason'] == 'cancel':
                db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE status='interrupted'", (time.time(),))
            db.execute('UPDATE team SET status=?', ('cancelled' if team['reason'] == 'cancel' else 'paused',))
            return self._view(db)

    def resolve(self, job_id, action, *, expected_generation=None, expected_attempt=None):
        if action not in ('retry', 'discard', 'publish'):
            raise ValueError('Выберите повторить поручение, снять его или опубликовать сохранённый результат.')
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            self._expect(db, expected_generation, row, expected_attempt)
            if self._team(db)['status'] != 'paused' or row['status'] != 'interrupted':
                raise ValueError('Решение принимается для прерванного поручения после подтверждённой паузы.')
            review = repair.records(db, job_id).get(job_id)
            if review and review['status'] == 'interrupted':
                if action == 'publish':
                    raise ValueError('Тест прерван. Сверьте кандидат и назначьте повтор проверки.')
                if action == 'retry':
                    if _file_hash(self._target(row)) != row['candidate_hash']:
                        raise ValueError('Проверяемый файл изменился с момента выполнения хода.')
                    repair.retry_check(db, job_id, interrupted=True)
                    db.execute("UPDATE jobs SET status='completed',error=NULL,updated_at=? WHERE id=?", (time.time(), job_id))
                else:
                    db.execute("UPDATE repair_attempts SET status='discarded' WHERE job_id=?", (job_id,))
                    db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (time.time(), job_id))
                return self._job(db, job_id)
            if action == 'publish':
                if row['result'] is None:
                    raise ValueError('Эта попытка не успела сохранить результат.')
                return self._publish(db, row)
            if action == 'retry':
                if row['assigned_file'] and _file_hash(self._target(row)) != row['baseline_hash']:
                    raise ValueError('Рабочий файл изменился. Перед новым запуском требуется сверка исходника.')
                db.execute("""INSERT INTO previous_attempts SELECT boundary, id, attempt, baseline_hash,
                           candidate, candidate_hash, result, error, 'retry', ? FROM jobs WHERE id=?""",
                           (time.time(), job_id))
                if review:
                    repair.save_usage(db, job_id, {})
                db.execute("""UPDATE jobs SET status='queued', generation=NULL, boundary=NULL, candidate=NULL,
                           candidate_hash=NULL, result=NULL, error=NULL, updated_at=? WHERE id=?""", (time.time(), job_id))
            else:
                db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (time.time(), job_id))
            return self._job(db, job_id)

    def resume(self, *, expected_generation=None):
        with self._transaction(write=True) as db:
            self._expect(db, expected_generation)
            team = self._team(db)
            if team['status'] == 'running':
                return self._view(db)
            if team['status'] != 'paused' or db.execute("SELECT 1 FROM jobs WHERE status='interrupted'").fetchone():
                raise ValueError('Для продолжения подтвердите остановку и примите решения по всем прерванным поручениям.')
            db.execute("UPDATE team SET status='running', generation=generation+1, reason=NULL")
            return self._view(db)

    @staticmethod
    def _clear(db):
        repair.clear_handoffs(db)
        return db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE status='queued'", (time.time(),)).rowcount

    def clear_queue(self, *, expected_generation=None):
        with self._transaction(write=True) as db:
            self._expect(db, expected_generation)
            return self._clear(db)

    def cancel_queued(self, job_id, *, expected_generation=None, expected_attempt=None):
        with self._transaction(write=True) as db:
            row = self._job(db, job_id)
            self._expect(db, expected_generation, row, expected_attempt)
            if row['status'] == 'cancelled':
                return row
            if row['status'] != 'queued':
                raise ValueError('Снять из очереди можно только ожидающее поручение.')
            db.execute("UPDATE jobs SET status='cancelled', updated_at=? WHERE id=?", (time.time(), job_id))
            return self._job(db, job_id)
