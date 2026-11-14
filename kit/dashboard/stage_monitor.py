"""Монитор одного этапа. Связывает документы проектной базы с журналом исполнения."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlencode

from ..journal import TaskJournal
from ..project_revisions import overview_revisions
from ..project_vault import BINDING_FILE, ProjectVault
from ..project_workspace import SECTIONS, section_paths
from ..role_tasks import ANALYST, ENGINEERS, MANAGER, assignments, missing_checks
from ..stage_collaboration import apply_action, read_collaboration
from ..stage_decisions import decide, decision_view, resource_forecast
from ..stage_risks import risk_register
from .stage_documents import save_document
from ..stage_resource_ledger import record_resource, resource_snapshot, resource_view
from ..task_control_state import plain_path

ROLE_KEYS = {MANAGER: 'manager', ANALYST: 'analyst', ENGINEERS: 'engineer'}
SECTION_TITLES = {section.key: section.title for section in SECTIONS}
RUN_FIELDS = ('event_id', 'at', 'scope', 'status', 'tests', 'failures', 'errors', 'skipped')


def _source_url(stage, feature, section, revision=None):
    query = dict(stage=stage, feature=feature, section=section)
    if revision is not None:
        query['revision'] = revision
    return '/api/stage/source?' + urlencode(query)


def _card(vault, record, stage, latest, revisions):
    key = record['key']
    snapshot = latest.get(key, {})
    revision = snapshot.get('revision')
    card = dict(key=key, title=record['title'], description=record['description'],
                revision=revision, revisions=list(revisions.get(key, {})),
                saved_sections=revisions.get(key, {}), documents={},
                archive=_source_url(stage, key, 'card', revision) if revision else None,
                tasks=[], checks=[], progress={role: dict(open=0, done=0) for role in (*ROLE_KEYS.values(), 'all')})
    for section, path in section_paths(vault, key).items():
        url = _source_url(stage, key, section)
        card['documents'][section] = url
        try:
            raw = path.read_bytes()
            note = raw.decode('utf-8-sig')
        except (ValueError, OSError):
            card['checks'].append(dict(section=section, text='Исходный документ недоступен'))
            continue
        for task in assignments(note):
            state = 'done' if task.done else 'open'
            card['progress']['all'][state] += 1
            roles = [ROLE_KEYS[owner] for owner in task.roles]
            for role in roles:
                card['progress'][role][state] += 1
            if not task.done:
                card['tasks'].append(dict(roles=roles, text=task.text, section=section))
        for message in missing_checks(section, note):
            card['checks'].append(dict(section=section, text=message))
        archived = snapshot.get('documents', {}).get(path.relative_to(vault.path).as_posix())
        if section in ('requirements', 'bdd', 'nfr') and archived and hashlib.sha256(raw).hexdigest() != archived['sha256']:
            card['checks'].append(dict(section=section, text=f'Рабочий документ изменён после редакции {revision}'))
    return card


def stage_view(root: Path, selected: str | None):
    result = dict(project=dict(id=None, name=root.name), stages=[], stage=None, features=[],
                  threads=[], traces=[], decisions=None, resources=None, risks=None)
    if not plain_path(root / BINDING_FILE).is_file():
        if selected:
            raise ValueError('Для выбранного этапа требуется привязанная база проекта.')
        return result
    vault = ProjectVault.load(root)
    with vault.reading() as db:
        result['project'] = dict(db.execute('SELECT id, name FROM project WHERE singleton=1').fetchone())
        records = [dict(row) for row in db.execute('SELECT * FROM records ORDER BY rowid')]
        latest, pending = overview_revisions(db)
        revisions = {}
        if latest:
            # В ранних редакциях сохранялась только карточка. Получаем имена разделов без чтения их текстов.
            history = db.execute("""
                SELECT h.record_key, h.number, d.key FROM revision_history h
                JOIN records r ON r.key=h.record_key
                LEFT JOIN json_each(h.document, '$.documents') d ON true
                WHERE ? IS NULL OR r.key=? OR r.parent=?
                ORDER BY h.number DESC""", (selected, selected, selected))
            section_keys = {section.title + '.md': section.key for section in SECTIONS}
            for key, number, path in history:
                saved = revisions.setdefault(key, {}).setdefault(number, [])
                section = section_keys.get(path.rsplit('/', 1)[-1]) if path else None
                if section:
                    saved.append(section)
    stages = {rec['key']: rec for rec in records if rec['kind'] == 'stage'}
    result['stages'] = [dict(key=k, title=v['title'], revision=latest.get(k, {}).get('revision')) for k, v in stages.items()]
    if selected is None:
        selected = next(iter(stages), None)
    if selected is None:
        return result
    if selected not in stages:
        raise ValueError('Этого этапа нет в выбранном проекте.')
    result['stage'] = _card(vault, stages[selected], selected, latest, revisions)
    decisions = decision_view(vault, selected)
    result['decisions'] = decisions
    result['resources'] = resource_view(vault, selected, decisions['forecast'])
    result['risks'] = risk_register(vault, selected, decisions['forecast'])
    for risk in result['risks']['rows']:
        path = vault.path / 'Документы' / risk['key'] / 'Риски.md'
        risk['obsidian_url'] = 'obsidian://open?path=' + quote(str(path), safe='')
    forecasts = {item['key']: item for item in decisions['forecast']['features']}
    for record in records:
        if record['parent'] != selected:
            continue
        card = _card(vault, record, selected, latest, revisions)
        forecast = forecasts.get(card['key'], {})
        card['estimate'] = dict(total=forecast.get('total_hours'), base=forecast.get('base_hours'),
                                reserve=forecast.get('reserve_hours'), known=forecast.get('known_hours'))
        card['request'] = pending.get(card['key'], {}).get('task_id')
        result['features'].append(card)
    result.update(read_collaboration(vault, selected))
    for trace in result['traces']:
        key = trace.get('feature') or selected
        trace['working_url'] = _source_url(selected, key, trace['section'])
        trace['card_url'] = _source_url(selected, key, 'card', trace['revision'])
        trace['archive_url'] = _source_url(selected, key, trace['section'], trace['revision'])
        if trace.get('test_event_id'):
            trace['log_url'] = '/api/stage/log?' + urlencode(dict(task=trace['task_id'], event=trace['test_event_id']))
    return result


def command(root: Path, data: dict, operation: str):
    vault = ProjectVault.load(root)
    if data.get('project_id') != vault.project_id:
        raise ValueError('Изменилась привязка проекта. Обновите страницу этапа.')
    values = data.copy()
    stage = values.pop('stage', None)
    if not isinstance(stage, str) or vault.record(stage)['kind'] != 'stage':
        raise ValueError('Выберите этап проекта.')
    if operation == 'decision':
        return decide(vault, stage, values)
    if operation == 'document':
        return save_document(vault, stage, values)
    if operation == 'resource':
        return record_resource(vault, stage, resource_forecast, values)
    return apply_action(vault, stage, values)


def resource_inputs(root, stage, version):
    if not re.fullmatch(r'[1-9][0-9]{0,8}', version):
        raise ValueError('Укажите номер сохранённой версии ресурсного плана.')
    return resource_snapshot(ProjectVault.load(root), stage, int(version))


def source(vault, stage, feature, section, revision=None):
    record = vault.record(feature)
    if vault.record(stage)['kind'] != 'stage' or (record['parent'] or record['key']) != stage:
        raise ValueError('Выбранный документ принадлежит другому этапу.')
    if section not in SECTION_TITLES and section != 'card':
        raise ValueError('Неизвестный раздел.')
    if revision is None:
        path = vault.card(feature) if section == 'card' else section_paths(vault, feature)[section]
        return path.read_text(encoding='utf-8-sig')
    if not re.fullmatch('[1-9][0-9]{0,8}', revision):
        raise ValueError('Некорректный номер редакции.')
    with vault.reading() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='revision_history'").fetchone():
            raise FileNotFoundError('У этого проекта ещё нет сохранённых редакций.')
        row = db.execute('SELECT document FROM revision_history WHERE record_key=? AND number=?',
                         (feature, int(revision))).fetchone()
    if row:
        saved = json.loads(row[0])
        if section == 'card':
            return saved['card_text']
        name = f'Документы/{feature}/{SECTION_TITLES[section]}.md'
        if name in saved.get('documents', {}):
            return saved['documents'][name]['text']
    raise FileNotFoundError('Снимок выбранного раздела отсутствует в этой редакции.')


def run_events(root, task):
    journal = TaskJournal(root)
    plain_path(journal.task_dir(task) / 'events.jsonl')
    return [event for event in journal.read(task) if event['kind'] == 'test' and event.get('event_id')]


def run_log(root, task, event_id):
    event = next((e for e in run_events(root, task) if e['event_id'] == event_id), None)
    if not event or not re.fullmatch(r'test-[0-9a-f]{32}\.log', event.get('log') or ''):
        raise FileNotFoundError('У этого события нет сохранённого лога проверки.')
    path = plain_path(TaskJournal(root).task_dir(task) / event['log'])
    # Большой журнал можно читать с диска. Экран показывает окончание с итогом прогона.
    limit = 2 * 1024 * 1024
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - limit))
        return ('Показаны последние 2 МиБ.\n\n' if size > limit else '') + stream.read(limit).decode('utf-8', 'replace')
