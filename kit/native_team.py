"""Запуск и остановка поручений Codex и Claude в отдельной команде проекта."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
import time
from uuid import UUID, uuid4

from . import chief_mode, native_repair
from .native_team_state import NativeTeamState
from .project_vault import publish_new
from .runtimes import native_process, native_provider_events, native_worker
from .task_control_state import operation_lock, plain_path
from .task_metrics import TaskMetrics


MAX_OUTPUT = 12 * 1024 * 1024
TURN_TIMEOUT = 600


def team_for(root, task):
    state = NativeTeamState(root, task)
    return state if plain_path(state.path).is_file() else None


def _team(root, task):
    state = team_for(root, task)
    if state is None:
        raise ValueError('У задачи нет управляемой команды Codex или Claude.')
    return state


def create(root, task, agent):
    root = Path(root).resolve()
    with operation_lock(root, 'chief-mode-switch'):
        chief_mode.require_enabled(root)
        if agent not in ('codex', 'claude'):
            raise ValueError('Выберите Codex или Claude.')
        state = NativeTeamState(root, task)
        if state.path.is_file():
            return state.initialize(agent)
        if not native_process.supported():
            raise ValueError('Среда не подтвердила изоляцию семейства процессов.')
        directory = state.directory
        marker = plain_path(directory / 'native-team-owner.json')
        owner = {'agent':agent, 'task':task, 'project':str(root)}
        # Не подключаем управление к чужой или уже работающей сессии CLI.
        journal = plain_path(directory / 'events.jsonl')
        if journal.exists() and not marker.exists():
            raise ValueError('Этот ID уже занят другой задачей. Укажите новый ID команды.')
        directory.mkdir(parents=True, exist_ok=True)
        if not publish_new(marker, json.dumps(owner, ensure_ascii=False).encode()) and json.loads(marker.read_bytes()) != owner:
            raise ValueError('Эта команда уже принадлежит другой задаче.')
        metrics = TaskMetrics(root)
        if not journal.is_file():
            from .loop_guards import DEFAULT_LIMITS
            with metrics.lock(task):
                metrics.append(task, 'start', title='Команда Чифа · ' + task, session=None, native_agent=agent,
                               limits=dict(DEFAULT_LIMITS), track_delegations=False, ux_review_required=False)
        return state.initialize(agent)


def enqueue(root, task, role, instructions, *, assigned_file=None, contracts=(), operation_id=None,
            test_command=None, max_attempts=None, fixer_attempts=None):
    root = Path(root).resolve()
    chief_mode.require_enabled(root)
    state = _team(root, task)
    identifier = str(UUID(operation_id)) if operation_id else str(uuid4())
    agent = state.operator_view(include_jobs=False)['agent']
    # Представление пути в журнале одно и то же при запуске из PowerShell и из sh.
    assigned_file = assigned_file.replace('\\', '/') if isinstance(assigned_file, str) else assigned_file
    contracts = tuple(name.replace('\\', '/') if isinstance(name, str) else name for name in contracts)
    workspace = root if role == 'chief' else plain_path(state.directory / 'workspaces' / identifier)
    if role != 'chief':
        workspace.mkdir(parents=True, exist_ok=True)
    plan_path = native_repair.assignment_path(state, identifier)
    overrides = {k: v for k, v in [('maxAttempts', max_attempts), ('fixerMaxAttempts', fixer_attempts)] if v is not None}
    if (test_command is not None and role != 'executor') or (overrides and test_command is None):
        raise ValueError('Проверку и бюджет задают для исходного поручения исполнителя с тестом.')
    with operation_lock(root, 'native-work-' + identifier):
        if plan_path.exists():
            plan = json.loads(plan_path.read_bytes())
            identity = (plan['role'], plan['instructions'], (plan['assigned'] or {}).get('path'),
                        [entry['path'] for entry in plan['contracts']])
            if identity != (role, instructions, assigned_file, list(contracts)):
                raise ValueError('Этот UUID уже использован для другого поручения.')
            existing = plan.get('verification')
            if test_command is not None:
                command = native_repair.validate_command(test_command, contracts)
                if not existing or command != existing['command'] or any(existing['policy'][k] != v for k, v in overrides.items()):
                    raise ValueError('Этот UUID уже связан с другой проверкой или бюджетом.')
            elif existing:
                raise ValueError('Для повтора поручения укажите исходную команду теста.')
        else:
            plan = native_worker.prepare(agent, root, assigned_file, instructions, contracts=contracts, role=role)
            selected_model = os.environ.get(native_worker.MODEL_VARIABLE[agent])
            if selected_model:
                plan['model'] = selected_model
            if test_command is not None:
                plan['verification'] = native_repair.verification_spec(root, test_command, contracts, overrides)
            if role != 'chief':
                native_repair.freeze_contracts(workspace, [plan['assigned'], *plan['contracts']])
                plan['workdir'] = str(workspace)
            native_repair.freeze_file(plan_path, json.dumps(plan, ensure_ascii=False).encode())
        with operation_lock(root, 'chief-mode-switch'):
            chief_mode.require_enabled(root)
            return state.enqueue(role=role, instructions=instructions, workspace=workspace, assigned_file=assigned_file,
                                 contracts=contracts, operation_id=identifier,
                                 verification=(plan.get('verification') or {}).get('policy'))


def stop(state, reason, *, expected_generation=None):
    view = state.begin_stop(reason, expected_generation=expected_generation)
    return confirm_stopping(state, view)


def confirm_stopping(state, view):
    if view['status'] != 'stopping':
        return view
    for boundary in view['active']:
        if not native_process.stop_and_confirm(boundary):
            raise RuntimeError('ОС пока не подтвердила остановку всех процессов. Повторите сверку команды.')
    try:
        return state.confirm_stop(view['generation'], view['active'])
    except ValueError:
        # Другой поток мог повысить причину остановки до квоты или отмены.
        current = state.view()
        if current['generation'] == view['generation']:
            raise
        return confirm_stopping(state, current)


def pause_project(root):
    """При смене режима обрабатываем только задачи и сессии данного проекта."""
    from .dashboard.controls import TaskControls
    from .native_chief_process import pause_direct_project
    from .task_control_state import read_state
    root = Path(root).resolve()
    pause_direct_project(root)
    metrics = TaskMetrics(root)
    for folder in sorted((root / '.metrics').iterdir()):
        if not folder.is_dir() or folder.name.startswith('.'):
            continue
        state = team_for(root, folder.name) if (folder / 'native-team.sqlite3').is_file() else None
        if state is not None:
            stop(state, 'pause')
        elif (folder / 'events.jsonl').is_file():
            events = metrics.read(folder.name)
            if not events[0].get('session') or any(event['kind'] == 'finish' for event in events):
                continue
            if read_state(root, folder.name).get('status') in ('paused', 'cancelled', 'deleted'):
                continue
            controls = TaskControls(root)
            controls.request(folder.name, 'pause')
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                status = controls.view(folder.name)
                if status.get('status') == 'paused':
                    break
                if status.get('status') == 'error':
                    raise ValueError(status.get('error') or 'Служба не подтвердила остановку команды.')
                time.sleep(.15)
            else:
                raise TimeoutError('Служба ещё не подтвердила остановку команды ' + folder.name)


def _execute(state, job):
    identifier, boundary = job['id'], job['boundary']
    try:
        plan = json.loads(plain_path(state.directory / 'assignments' / (identifier + '.json')).read_bytes())
        incidents = state.job(identifier).get('provider_incidents', [])
        model = plan.get('model') or next((item['model'] for item in reversed(incidents) if item['model']), None)
        with native_worker.invocation(plan, model=model) as call, TemporaryFile() as inp, TemporaryFile() as out, TemporaryFile() as err:
            inp.write(call['stdin'])
            inp.seek(0)
            policy = (plan.get('verification') or {}).get('policy', {})
            duration = policy.get('fixerMaxSeconds' if job['role'] == 'bug_fixer' else 'maxSeconds', TURN_TIMEOUT)
            deadline = time.monotonic() + duration
            code = native_process.run_owned(call['command'], cwd=call['cwd'], env=call['env'], stdin=inp,
                stdout=out, stderr=err, boundary=boundary, admission=lambda: state.admit(identifier, boundary),
                should_stop=lambda: time.monotonic() >= deadline or not state.is_running(identifier, boundary))
            out.seek(0)
            output = out.read(MAX_OUTPUT + 1)
            if len(output) > MAX_OUTPUT:
                raise ValueError('Ответ CLI превысил допустимый размер.')
            incident = native_provider_events.observe(plan, output, code)
            if incident:
                selected = call['env'].get(native_worker.MODEL_VARIABLE[plan['agent']]) or incident.get('model')
                stopping = state.begin_provider_stop(identifier, boundary, incident['reason'],
                                                     partial=incident['partial'], model=selected)
                if stopping:
                    confirm_stopping(state, stopping)
                return
            if not state.is_running(identifier, boundary):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError('Истёк срок исполнения поручения.')
            result = native_worker.parse_result(plan, output, code)
            candidate = native_worker.candidate_bytes(plan, result)
            result.pop('candidate', None)
            if candidate is not None:
                result.pop('text', None)
            state.checkpoint(identifier, boundary, candidate=candidate, result=result)
            state.commit(identifier, boundary)
    except (OSError, ValueError, RuntimeError) as error:
        if native_process.stop_and_confirm(boundary):
            state.fail(identifier, boundary, str(error))
        else:
            stop(state, 'recovery')


def drain(root, task, *, parallel=2, expected_generation=None):
    if type(parallel) is not int or not 1 <= parallel <= 8:
        raise ValueError('Выберите от одного до восьми одновременно работающих участников.')
    chief_mode.require_enabled(root)
    state = _team(root, task)
    lock_name = 'native-dispatch-' + hashlib.sha256(task.encode()).hexdigest()[:32]
    with operation_lock(state.root, lock_name):
        view = state.view()
        if expected_generation is not None and expected_generation != view['generation']:
            raise ValueError('Команда уже перешла в другое поколение. Запустите очередь из обновлённого рабочего места.')
        if view['active'] or view['status'] == 'stopping':
            return stop(state, 'recovery', expected_generation=view['generation'])
        def consume():
            while True:
                job = state.claim(expected_generation=view['generation'])
                if job is None:
                    return
                _execute(state, job)
        # Сначала сверяем уже сохранённые результаты предыдущего диспетчера.
        # Только диспетчер выдаёт преемников, рабочие потоки берут готовые поручения.
        native_repair.review_completed(state, view['generation'])
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            while True:
                for future in [pool.submit(consume) for _ in range(parallel)]:
                    future.result()
                if not native_repair.review_completed(state, view['generation']):
                    break
    return state.view()


def _start_background(state, generation):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
    with plain_path(state.directory / 'native-manager.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-B', '-m', 'kit.native_team', '--root', str(state.root),
                          '--task', state.task, 'run', '--generation', str(generation)], cwd=Path(__file__).resolve().parents[1],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    return state.view()


def integrate(state, identifier, *, expected_generation=None, expected_attempt=None):
    """Оператор переносит проверенный файл, пока основа и контракты совпадают со снимками."""
    from .native_team_state import replace_source
    with operation_lock(state.root, 'chief-mode-switch'), state.integration_job(
            identifier, expected_generation=expected_generation, expected_attempt=expected_attempt) as job:
        chief_mode.require_enabled(state.root)
        plan = json.loads(plain_path(state.directory / 'assignments' / (job['id'] + '.json')).read_bytes())
        target = plain_path(state.root / job['assigned_file'])
        for entry in plan['contracts']:
            if hashlib.sha256(plain_path(state.root / entry['path']).read_bytes()).hexdigest() != entry['sha256']:
                raise ValueError('Контракт изменился. Пересмотрите результат поручения.')
        content = plain_path(Path(job['workspace']) / job['assigned_file']).read_bytes()
        if hashlib.sha256(content).hexdigest() != job['candidate_hash']:
            raise ValueError('Проверенная рабочая копия изменилась.')
        original = job['review']['original_hash'] if job.get('review') else job['baseline_hash']
        if hashlib.sha256(target.read_bytes()).hexdigest() != job['candidate_hash']:
            replace_source(target, original, content)
        receipt = dict(job=job['id'], path=job['assigned_file'], previous_hash=original,
                       hash=job['candidate_hash'])
        native_repair.freeze_file(state.directory / 'assignments' / (job['id'] + '.integrated.json'),
                                  json.dumps(receipt, ensure_ascii=False).encode())
    return state.view()


def perform(root, task, action, *, job_id=None, expected_generation=None, expected_attempt=None):
    state = _team(root, task)
    if action in ('pause', 'cancel', 'recover', 'limit'):
        return stop(state, {'recover':'recovery'}.get(action, action), expected_generation=expected_generation)
    if action == 'clear':
        state.clear_queue(expected_generation=expected_generation)
    elif action == 'drop':
        state.cancel_queued(job_id, expected_generation=expected_generation, expected_attempt=expected_attempt)
    elif action in ('discard', 'publish', 'retry'):
        state.resolve(job_id, action, expected_generation=expected_generation, expected_attempt=expected_attempt)
    else:
        chief_mode.require_enabled(root)
        if action == 'resume':
            with operation_lock(state.root, 'chief-mode-switch'):
                chief_mode.require_enabled(root)
                return state.resume(expected_generation=expected_generation)
        if action == 'run':
            generation = state.operator_view(include_jobs=False)['generation']
            if expected_generation is not None and expected_generation != generation:
                raise ValueError('Состояние команды изменилось. Обновите список поручений.')
            return _start_background(state, generation)
        if action == 'integrate':
            return integrate(state, job_id, expected_generation=expected_generation, expected_attempt=expected_attempt)
        if action == 'recheck':
            state.recheck(job_id, expected_generation=expected_generation, expected_attempt=expected_attempt)
            return state.view()
        raise ValueError('Неизвестная операция команды.')
    return state.view()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--task', required=True)
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('create').add_argument('--agent', choices=('codex','claude'), required=True)
    add = actions.add_parser('enqueue')
    add.add_argument('--role', choices=('chief','executor','bug_fixer'), required=True)
    add.add_argument('--file')
    add.add_argument('--contract', action='append', default=[])
    add.add_argument('--instructions', required=True)
    add.add_argument('--id')
    add.add_argument('--max-attempts', type=int, help='Общий бюджет неудачных реализаций поручения')
    add.add_argument('--fixer-attempts', type=int, help='Лимит неудачных попыток исправителя')
    add.add_argument('--test', nargs=argparse.REMAINDER, help='Команда приёмочного unittest и его аргументы')
    run = actions.add_parser('run')
    run.add_argument('--parallel', type=int, default=2)
    run.add_argument('--generation', type=int)
    partial = actions.add_parser('partial', help='Сохранить копию файла, полученного до отказа провайдера')
    partial.add_argument('--job', required=True)
    partial.add_argument('--attempt', type=int, required=True)
    partial.add_argument('--output', type=Path, required=True, help='Новое имя файла для сверки')
    for name in ('status','pause','resume','cancel','recover','clear','limit'):
        actions.add_parser(name)
    for name in ('drop','discard','publish','retry','integrate','recheck'):
        actions.add_parser(name).add_argument('--job', required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == 'create':
            result = create(args.root, args.task, args.agent)
        elif args.action == 'enqueue':
            result = enqueue(args.root, args.task, args.role, args.instructions, assigned_file=args.file,
                             contracts=args.contract, operation_id=args.id, test_command=args.test,
                             max_attempts=args.max_attempts, fixer_attempts=args.fixer_attempts)
        elif args.action == 'status':
            result = _team(args.root, args.task).view()
        elif args.action == 'partial':
            content = _team(args.root, args.task).provider_partial(args.job, args.attempt)
            target = plain_path(args.output.absolute())
            if not publish_new(target, content):
                raise ValueError('Файл уже существует. Укажите новое имя копии для сверки.')
            result = dict(path=str(target), bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        elif args.action == 'run':
            result = drain(args.root, args.task, parallel=args.parallel, expected_generation=args.generation)
        else:
            result = perform(args.root, args.task, args.action, job_id=getattr(args, 'job', None))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
