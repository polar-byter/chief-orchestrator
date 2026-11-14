"""Проверка готовых кандидатов и передача отклонённого исходника исправителю."""
from codecs import BOM_UTF8
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from . import chief_mode
from .execution_policy import normalize_policy
from .native_candidate_check import validate_command, verify
from .project_vault import publish_new
from .runtime_selection import load_project_model_choices
from .runtimes import native_process, native_worker
from .task_control_state import operation_lock, plain_path


def verification_spec(root, command, contracts, overrides):
    policy = normalize_policy(load_project_model_choices(root).get('execution'))
    policy.update(overrides)
    return {'command': validate_command(command, contracts), 'policy': normalize_policy(policy)}


def assignment_path(state, identifier):
    return plain_path(state.directory / 'assignments' / (str(UUID(identifier)) + '.json'))


def freeze_file(path, content):
    """Повтор после обрыва допускает только те же байты."""
    if not publish_new(path, content) and plain_path(path).read_bytes() != content:
        raise ValueError('Снимок поручения изменился. Сверьте рабочую копию: ' + str(path))


def freeze_contracts(workspace, entries):
    for entry in entries:
        raw = (BOM_UTF8 if entry['bom'] else b'') + entry['text'].encode('utf-8')
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Не удалось подтвердить снимок ' + entry['path'])
        freeze_file(plain_path(workspace / entry['path']), raw)


def check_candidate(state, job_id, generation):
    job = state.job(job_id)
    boundary = str(uuid4())
    if not state.begin_check(job_id, boundary, expected_generation=generation):
        return
    try:
        if not native_process.stop_and_confirm(job['boundary']):
            raise RuntimeError('Нет подтверждения остановки исполнителя.')
        plan = json.loads(assignment_path(state, job_id).read_bytes())
        evidence = verify(state, job, plan, boundary=boundary,
                          admission=lambda: state.check_admission(job_id, boundary),
                          should_stop=lambda: not state.check_running(job_id, boundary))
    except (ValueError, OSError, RuntimeError) as error:
        evidence = {'verdict': 'blocked', 'diagnostic': str(error)[:500], 'candidate_hash': job['candidate_hash']}
    if state.check_running(job_id, boundary):
        if not native_process.stop_and_confirm(boundary):
            raise RuntimeError('Остановка процесса проверки пока не подтверждена. Нужна сверка команды.')
        state.finish_check(job_id, boundary, evidence)


def handoff(state, job_id, generation):
    job = state.job(job_id)
    review = state.review_info(job_id)
    # У каждой отклонённой реализации может быть ровно один непосредственный преемник.
    child = str(uuid5(UUID(job_id), 'chief/bug-fixer'))
    plan = json.loads(assignment_path(state, job_id).read_bytes())
    workspace = plain_path(state.directory / 'workspaces' / child)
    target = job['assigned_file']
    with operation_lock(state.root, 'native-work-' + child):
        candidate = plain_path(Path(job['workspace']) / target).read_bytes()
        if hashlib.sha256(candidate).hexdigest() != review['check']['candidate_hash']:
            raise ValueError('Отклонённый кандидат изменился после теста. Сверьте рабочую копию.')
        workspace.mkdir(parents=True, exist_ok=True)
        freeze_file(plain_path(workspace / target), candidate)
        freeze_contracts(workspace, plan['contracts'])
        contracts = [entry['path'] for entry in plan['contracts']]
        successor = native_worker.prepare(plan['agent'], workspace, target, plan['instructions'],
                                          role='bug_fixer', contracts=contracts)
        successor['verification'] = plan['verification']
        if plan.get('model'):
            successor['model'] = plan['model']
        successor['repair'] = dict(previous_job=job_id, number=review['number'] + 1,
                                   diagnostic=review['check']['diagnostic'],
                                   test_name=review['check'].get('test_name'), tests=review['check'].get('tests'),
                                   failures=review['check'].get('failures'), rejected_sha256=job['candidate_hash'],
                                   remaining_total=review['remaining_total'], remaining_fixer=review['remaining_fixer'])
        freeze_file(assignment_path(state, child), json.dumps(successor, ensure_ascii=False).encode())
        with operation_lock(state.root, 'chief-mode-switch'):
            chief_mode.require_enabled(state.root)
            return state.enqueue(role='bug_fixer', instructions=plan['instructions'], workspace=workspace,
                                 assigned_file=target, contracts=contracts, operation_id=child, previous_job=job_id,
                                 verification=review['policy'], expected_generation=generation)


def review_completed(state, generation):
    """После рестарта продолжаем с завершённой записи, без нового запроса прежнему исполнителю."""
    for job_id in state.pending_checks():
        check_candidate(state, job_id, generation)
    queued = False
    for job_id in state.pending_handoffs():
        handoff(state, job_id, generation)
        queued = True
    return queued
