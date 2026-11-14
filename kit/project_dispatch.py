"""Передача сохранённой заявки в корневую сессию Чифа выбранной среды."""
from __future__ import annotations

import json
from pathlib import Path

from . import native_chief
from .chief_mode import require_enabled
from .model_switch import references
from .project_vault import ProjectVault, publish_new
from .runtime_selection import create_session_gateway, load_project_model_choices
from .task_control_state import operation_lock, plain_path
from .task_metrics import TaskMetrics, session_id


def _chief_model(root: Path) -> dict:
    reference = references(root, load_project_model_choices(root))['masterChief']
    provider, name = reference.split('/', 1)
    name, separator, variant = name.partition('#')
    model = {'providerID': provider, 'id': name}
    if separator:
        model['variant'] = variant
    return model


def _prompt(vault, request, session, *, native=False):
    introduction = (
        f"Журнал {request['task_id']} уже начат для корневой сессии {session}. "
        "Выполни запрос в указанном пользователем объёме. На вопрос или просьбу подтвердить "
        "получение ответь в этой сессии. Для реализации следуй AGENTS.md, проверь зависимости "
        "и результат. После finish запиши проверку этой заявки через kit.project_docs accept --task.\n"
    )
    card = request['card_text'] if native else vault.card(request['key'])
    if native:
        introduction = ("Выполни один запрос из сохранённой заявки. На вопрос ответь в этой сессии. "
                        "При реализации следуй инструкциям проекта и укажи выполненные проверки. "
                        "Приёмку редакции оформляет ответственный за проект.\n")
    return (
        f"Заявка {request['id']} проекта {vault.project_id}. {introduction}"
        f"Тип: {request['kind']}. Этап: {request['stage']}. Запись: {request['key']}. "
        f"Базовая редакция: {request['base_revision']}. "
        f"Связанные записи на момент запроса: {json.dumps(request['context'], ensure_ascii=False)}.\n"
        f"Карточка: {card}\n\nИзменение, переданное пользователем:\n"
        + request['text']
        + '\n\nДокументы на момент подачи заявки (сохранённые снимки):\n'
        + json.dumps(request.get('documents', {}), ensure_ascii=False)
        + ('\n\nРасчёт при постановке:\n' + request['estimate'] if 'estimate' in request else '')
    )


def dispatch_request(vault: ProjectVault, request: dict, *, resume=None) -> dict:
    require_enabled(vault.root)
    if request.get('project_id') != vault.project_id or request.get('status') != 'pending':
        raise ValueError('Нужна открытая заявка из этой базы проекта.')
    task = request['task_id']
    metrics = TaskMetrics(vault.root)
    directory = metrics.task_dir(task)
    with operation_lock(vault.root, task):
        with vault.reading() as db:
            row = db.execute('SELECT status, document FROM revision_requests WHERE id=?', (request['id'],)).fetchone()
            if not row or row['status'] != 'pending' or dict(json.loads(row['document']), status='pending') != request:
                raise ValueError('Передать можно только сохранённую открытую заявку этого проекта.')
        receipt = plain_path(directory / 'documentation-delivery.json')
        if receipt.exists():
            saved = json.loads(receipt.read_text(encoding='utf-8'))
            if saved.get('request') != request['id'] or saved.get('project') != vault.project_id:
                raise ValueError('Подтверждение принадлежит другой заявке.')
            native_chief.check_resume(saved, resume)
            return saved

        attempt = plain_path(directory / 'documentation-sending.json')
        if attempt.exists():
            started = json.loads(attempt.read_text(encoding='utf-8'))
            if started.get('agent') in native_chief.TRANSPORTS:
                native_chief.check_resume(started, resume)
                return native_chief.recover(vault, request, directory, started)
            raise ValueError('Передача уже началась, ответ не подтверждён. Сверьте исходную сессию в дашборде.')
        agent = native_chief.agent_for(vault)
        if agent != 'default':
            if plain_path(directory / 'events.jsonl').exists():
                raise ValueError('Для заявки уже создан журнал шлюза. Восстановите параметр --agent default для этой передачи.')
            return native_chief.deliver(vault, request, directory, agent, resume,
                                        _prompt(vault, request, None, native=True))
        if resume:
            raise ValueError('Для продолжения по UUID выберите Codex или Claude для новой заявки.')
        with operation_lock(vault.root, 'chief-mode-switch'):
            require_enabled(vault.root)
            return _deliver_gateway(vault, request, task, metrics, directory, receipt)


def _deliver_gateway(vault, request, task, metrics, directory, receipt):
    attempt = plain_path(directory / 'documentation-sending.json')
    gateway = create_session_gateway(vault.root)
    title = f"Чиф · {task}"
    existing_journal = (directory / 'events.jsonl').is_file()
    if existing_journal:
        start = metrics.read_events_if_work_allowed(task)[0]
        if start.get('title') != title:
            raise ValueError('ID журнала уже занят другой работой.')
        sid = session_id(start.get('session'))
        row = gateway.call('session.get', {'sessionID': sid})['data']
        if row.get('id') != sid:
            raise ValueError('Служба вернула другую корневую сессию.')
    else:
        model = _chief_model(vault.root)
        gateway.capabilities()
        # Если процесс оборвался между созданием сессии и записью журнала,
        # ищем только корневую сессию с уникальным названием этой заявки.
        matches = [row for row in gateway.sessions() if row.get('title') == title]
        if len(matches) > 1:
            raise ValueError('Обнаружено несколько сессий этой заявки. Сверьте их в дашборде.')
        row = matches[0] if matches else gateway.call('session.create', data={
            'parentID': None, 'title': title, 'agent': 'build', 'model': model,
            'location': {'directory': str(vault.root)},
        })['data']
        sid = session_id(row['id'])
    if (row.get('parentID') is not None or row.get('agent') != 'build' or row.get('title') != title
            or not row.get('location', {}).get('directory')
            or Path(row['location']['directory']).resolve() != vault.root.resolve()):
        raise ValueError('Служба не подтвердила корневую сессию этого проекта.')
    if not existing_journal:
        metrics.start(task, session=sid, title=title, track_delegations=True)

    message = 'msg_' + request['id'].removeprefix('doc-')
    details = {'request': request['id'], 'project': vault.project_id, 'task': task,
               'session': sid, 'message': message}
    encoded = json.dumps(details, ensure_ascii=False).encode('utf-8')
    if not publish_new(attempt, encoded):
        raise ValueError(f'Передача уже началась, ответ не подтверждён. Сверьте сессию {sid} в дашборде.')
    response = gateway.call('session.prompt', {'sessionID': sid}, {'id': message, 'text': _prompt(vault, request, sid)})
    if not isinstance(response, dict) or (response.get('data') or {}).get('id') != message:
        raise ValueError(f'Нет подтверждения приёма. Сверьте сессию {sid}.')
    publish_new(receipt, encoded)
    return details
