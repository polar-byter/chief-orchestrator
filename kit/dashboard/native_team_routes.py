"""Команды дашборда адресуются одной задаче и версиям, которые видел оператор."""
from __future__ import annotations

from base64 import b64encode
from uuid import UUID

from .. import chief_mode, native_team
from ..native_review_report import public_review
from ..task_control_state import plain_path
from .launcher import project_id


TEAM_ACTIONS = frozenset(('pause', 'resume', 'cancel', 'recover', 'run', 'clear'))
JOB_ACTIONS = frozenset(('drop', 'retry', 'discard', 'publish', 'integrate', 'recheck'))


def public_team(state):
    view = state.operator_view()
    for job in view['jobs']:
        if job.get('review'):
            job['review'] = public_review(job['review'])
        receipt = state.directory / 'assignments' / (job['id'] + '.integrated.json')
        job['integrated'] = (job['status'] == 'completed' and job['assigned_file'] is not None
                             and plain_path(receipt).is_file())
    return view


def control_view(view):
    stage = view['status']
    if stage == 'stopping':
        stage = 'cancelling' if view['reason'] == 'cancel' else 'pausing'
    return dict(status=stage, reason='provider_limit' if view['reason'] == 'limit' else view['reason'])


def public_mode(root):
    return dict(chief_mode.mode_state(root), project=project_id(root))


def command(root, endpoint, data):
    if not isinstance(data, dict) or data.get('project') != project_id(root):
        raise ValueError('Эта команда предназначена для другого проекта.')
    if endpoint == '/api/chief-mode':
        if set(data) != {'project', 'enabled'} or type(data['enabled']) is not bool:
            raise ValueError('Для переключения требуется состояние режима Чифа.')
        chief_mode.set_mode(root, data['enabled'])
        return public_mode(root)
    if endpoint == '/api/native-partial':
        if (set(data) != {'project', 'task', 'job_id', 'attempt'} or not isinstance(data['task'], str)
                or not isinstance(data['job_id'], str) or type(data['attempt']) is not int or data['attempt'] < 1):
            raise ValueError('Укажите задачу, поручение и номер сохранённой попытки.')
        state = native_team.team_for(root, data['task'])
        if state is None:
            raise ValueError('У задачи нет управляемой команды.')
        partial = state.provider_partial(data['job_id'], data['attempt'])
        return dict(content=b64encode(partial).decode('ascii'))
    if endpoint != '/api/native-team':
        raise ValueError('Неизвестная команда.')
    action = data.get('action')
    if not isinstance(action, str) or action not in TEAM_ACTIONS | JOB_ACTIONS:
        raise ValueError('Неизвестное действие для команды.')
    keys = {'project', 'task', 'action', 'generation'}
    if action in JOB_ACTIONS:
        keys.update(('job_id', 'attempt', 'boundary'))
    if set(data) != keys or not isinstance(data['task'], str) or type(data['generation']) is not int:
        raise ValueError('Укажите выбранную задачу и поколение команды.')
    state = native_team.team_for(root, data['task'])
    if state is None:
        raise ValueError('У выбранной задачи нет управляемой нативной команды.')
    if state.operator_view(include_jobs=False)['generation'] != data['generation']:
        raise ValueError('Состояние команды изменилось. Обновите страницу.')
    if action in JOB_ACTIONS:
        if type(data['attempt']) is not int or str(UUID(str(data['job_id']))) != data['job_id']:
            raise ValueError('Укажите UUID выбранного поручения и номер попытки.')
        current = state.job(data['job_id'])
        if current['boundary'] != data['boundary'] or current['attempt'] != data['attempt']:
            raise ValueError('Поручение изменилось. Откройте его повторно.')
    native_team.perform(root, data['task'], action, job_id=data.get('job_id'),
                        expected_generation=data['generation'], expected_attempt=data.get('attempt'))
    return public_team(state)
