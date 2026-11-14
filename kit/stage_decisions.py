"""Снимок трудозатрат этапа и решения о переоценке в проектной базе."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from fractions import Fraction
import hashlib
import json
from uuid import UUID

from .effort_estimate import display_hours, hours_value
from .estimate_documents import METHOD, WORK_COLUMNS, _table_lines, estimate_from_notes
from .project_workspace import SECTIONS
from .stage_collaboration import blocking_threads
from .task_control_state import plain_path


SHARED_RISK_COLUMNS = ('Общее событие', 'Риски фич', 'Где учтено')


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _hours(value: Fraction | None) -> float | None:
    if value is None:
        return None
    rounded = float(display_hours(abs(value)).replace(',', '.'))
    return -rounded if value < 0 else rounded


@dataclass
class StageInputs:
    documents: dict = field(default_factory=dict)
    signatures: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)

    def read(self, vault, key: str) -> dict:
        texts = {}
        for section in SECTIONS:
            name = f'Документы/{key}/{section.title}.md'
            try:
                raw = plain_path(vault.path / name).read_bytes()
                texts[section.key] = raw.decode('utf-8-sig')
                self.signatures[name] = hashlib.sha256(raw).hexdigest()
            except (OSError, ValueError):
                self.signatures[name] = None
                self.issues.append(f'{key} — недоступен документ «{section.title}».')
        self.documents[key] = texts
        return texts


def _shared_reserve(stage_note: str, allowances: dict) -> dict:
    """Для каждой повторной надбавки сохраняет адрес оставленного источника."""
    groups = _table_lines(stage_note, SHARED_RISK_COLUMNS) or []
    events, linked = set(), set()
    duplicates = {}
    for event, addresses, owner in groups:
        members = [value.strip() for value in addresses.split(',')]
        if (not event or event in events or len(set(members)) != len(members) or
                len(members) < 2 or owner not in members or set(members) & linked):
            raise ValueError('Общий риск — укажите уникальное событие, разные адреса фича/риск и один источник.')
        if any(address not in allowances for address in members):
            raise ValueError(f'{event} — проверьте адреса рисков в исходных фичах.')
        selected, hours = allowances[owner]
        if selected == 'без резерва' and any(allowances[key][0] != selected for key in members):
            raise ValueError(f'{event} — у источника «{owner}» отсутствует основание для резерва.')
        if hours is None:
            raise ValueError(f'{event} — в исходной фиче отсутствует решение по резерву.')
        for address in members:
            if address == owner:
                continue
            treatment, extra = allowances[address]
            if treatment == 'в o/m/p' or extra is None:
                raise ValueError(f'{event} — уточните учёт в {address}: последствие нельзя вычесть из O/M/P.')
            duplicates[address] = owner
        events.add(event)
        linked.update(members)
    return duplicates


def _resource_rows(key, estimate, risks):
    roles = {row.work.key: row.work.role for row in estimate.rows}
    for row in estimate.rows:
        work = row.work
        if row.mean is None and work.key == 'Не декомпозирована' and work.role == 'Не назначена':
            continue
        yield dict(key=f'{key}/{work.key}', role=work.role, hours=str(row.mean) if row.mean is not None else None,
                   o=work.optimistic, m=work.likely, p=work.pessimistic, basis=work.basis, unit=work.unit,
                   included_risks=list(work.included_risks))
    for risk in risks:
        if risk.treatment.casefold() != 'отдельно':
            continue
        owners = {roles.get(work, '') for work in risk.works}
        hours = estimate.reserves[risk.key]
        yield dict(key=f'{key}/@{risk.key}', role=next(iter(owners)) if len(owners) == 1 else '',
                   hours=str(hours) if hours is not None else None, risk=f'{key}/{risk.key}',
                   works=[f'{key}/{work}' for work in risk.works])


def _has_stage_work(note):
    # В старом шаблоне это был пример пустой строки. Реальная незаполненная работа влияет на полноту.
    placeholder = ['Не декомпозирована', 'Не назначена', 'чел·ч', 'Нет данных', 'Нет данных', 'Нет данных', 'Нет данных']
    return any(row[:7] != placeholder for row in _table_lines(note, WORK_COLUMNS) or [])


def _snapshot(vault, db, stage: str) -> dict:
    records = db.execute('SELECT * FROM records WHERE key=? OR parent=? ORDER BY key', (stage, stage)).fetchall()
    if not any(row['kind'] == 'stage' and row['key'] == stage for row in records):
        raise ValueError('Выберите существующий этап проекта.')
    has_history = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='revision_history'").fetchone()
    revisions = {}
    if has_history:
        revisions = dict(db.execute('SELECT record_key, max(number) FROM revision_history WHERE record_key IN '
                                    '(SELECT key FROM records WHERE key=? OR parent=?) GROUP BY record_key', (stage, stage)))
    inputs = StageInputs()
    rows, allowances, resource_work = [], {}, []
    work_scope = {}
    base, reserve = Fraction(), Fraction()
    known_work = False
    known_reserve = False
    for record in records:
        key = record['key']
        inputs.signatures[f'{key}/@редакция'] = revisions.get(key)
        inputs.signatures[f'{key}/@состав'] = _hash(dict(record))
        texts = inputs.read(vault, key)
        if key == stage:
            try:
                if not _has_stage_work(texts.get('effort', '')):
                    continue
            except ValueError as error:
                inputs.issues.append(str(error))
                continue
        issues = []
        known = total = feature_base = feature_reserve = None
        if 'effort' not in texts or 'risks' not in texts:
            issues.append('Нет исходных таблиц фичи.')
        else:
            try:
                estimate, risks = estimate_from_notes(texts['effort'], texts['risks'])
                resource_work.extend(_resource_rows(key, estimate, risks))
                work_scope[key] = [(row.work.key, row.work.role, row.work.unit, row.work.included_risks)
                                   for row in estimate.rows]
                issues.extend(estimate.missing)
                total = estimate.total
                if any(row.mean is not None for row in estimate.rows):
                    feature_base = estimate.base
                    known_work = True
                complete_reserve = bool(estimate.reserves) and all(x is not None for x in estimate.reserves.values())
                if complete_reserve or total is not None:
                    feature_reserve = estimate.reserve
                if feature_reserve is not None or estimate.reserve > 0:
                    known_reserve = True
                if feature_base is not None or estimate.reserve > 0:
                    known = estimate.base + estimate.reserve
                    base += estimate.base
                    reserve += estimate.reserve
                for risk in risks:
                    allowances[f'{key}/{risk.key}'] = (risk.treatment.casefold(), estimate.reserves[risk.key])
            except ValueError as error:
                issues.append(str(error))
        if key == stage:
            inputs.issues.extend(f'{key} — {message}' for message in issues)
        else:
            rows.append(dict(key=key, title=record['title'], revision=revisions.get(key), complete=total is not None,
                             known_hours=_hours(known), total_hours=_hours(total), base_hours=_hours(feature_base),
                             reserve_hours=_hours(feature_reserve), issues=issues))
    duplicates = {}
    try:
        duplicates = _shared_reserve(inputs.documents[stage].get('risks', ''), allowances)
        reserve -= sum((allowances[address][1] for address in duplicates), Fraction())
        known = base + reserve if known_work or known_reserve and reserve > 0 else None
    except ValueError as error:
        inputs.issues.append(str(error))
        # Если распределение общего риска спорно, отдельно показываем известную базу.
        known = None
        known_reserve = False
    complete_count = sum(row['complete'] for row in rows)
    resource_work = [work for work in resource_work if work.get('risk') not in duplicates]
    if len({work['key'] for work in resource_work}) != len(resource_work):
        inputs.issues.append('Адрес работы совпадает с резервом. Используйте разные ID для работы и надбавки @риск.')
        # У конфликтующего адреса нет однозначного исполнителя и зависимостей.
        addresses = {}
        for work in resource_work:
            addresses[work['key']] = work if work['key'] not in addresses else None
        resource_work = [work for work in addresses.values() if work is not None]
    total = known if resource_work and complete_count == len(rows) and not inputs.issues else None
    scope = {key: value for key, value in inputs.signatures.items()
             if not key.endswith('/Оценка трудозатрат.md')}
    return dict(stage_revision=revisions.get(stage), feature_count=len(rows), complete_count=complete_count,
                features=rows, resource_work=resource_work, shared_reserves=duplicates,
                risk_allowances={key: str(hours) if hours is not None else None for key, (_, hours) in allowances.items()},
                risk_source_hashes={row['key']: inputs.signatures.get(f"Документы/{row['key']}/Риски.md") for row in records},
                known_hours=_hours(known), total_hours=_hours(total), base_hours=_hours(base) if known_work else None,
                reserve_hours=_hours(reserve) if known_reserve else None,
                exact_hours=str(total) if total is not None else None, issues=inputs.issues,
                fingerprint=_hash(inputs.signatures), scope_fingerprint=_hash([scope, work_scope]), inputs=inputs.signatures,
                method=METHOD, unit='чел·ч', sources={key: {name: text for name, text in notes.items() if name in ('effort', 'risks')}
                                                     for key, notes in inputs.documents.items()})


def _default_policy() -> dict:
    return dict(mode='manual', version=0, max_delta_hours=None, valid_until=None, author=None)


@dataclass
class Ledger:
    policy: dict = field(default_factory=_default_policy)
    proposals: dict = field(default_factory=dict)
    approved: dict | None = None

    @classmethod
    def read(cls, db, stage):
        ledger = cls()
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_decision_events'").fetchone()
        if not exists:
            return ledger
        for row in db.execute('SELECT action, document FROM stage_decision_events WHERE stage=? ORDER BY rowid', (stage,)):
            item = json.loads(row['document'])
            if row['action'] == 'policy':
                ledger.policy = item
            else:
                ledger.proposals[item['id']] = item
                if item['status'] == 'accepted':
                    ledger.approved = item
        return ledger


def _changed(proposal, forecast):
    old = proposal['snapshot']['inputs']
    new = forecast['inputs']
    return sorted(key for key in old.keys() | new.keys() if key not in old or key not in new or old[key] != new[key])


def _card(proposal, forecast, approved_id):
    if not proposal:
        return None
    card = {key: value for key, value in proposal.items() if key != 'snapshot'}
    changed = _changed(proposal, forecast) if proposal['status'] == 'pending' else []
    if proposal['status'] == 'pending' and proposal['base_id'] != approved_id:
        changed.append('Решение РП по этапу')
    return dict(card, stale=bool(changed), changed_inputs=changed)


def _public_forecast(snapshot):
    return {k: v for k, v in snapshot.items() if k not in ('sources', 'inputs', 'exact_hours', 'scope_fingerprint')}


def resource_forecast(vault, db, stage):
    """Сверяет тот же состав работ, по которому РП принимает решение об оценке."""
    return _public_forecast(_snapshot(vault, db, stage))


def decision_view(vault, stage: str) -> dict:
    """Читает фактический прогноз независимо от того, принято ли решение РП."""
    with closing(vault._open('ro')) as db:
        db.execute('BEGIN')
        snapshot = _snapshot(vault, db, stage)
        ledger = Ledger.read(db, stage)
    approved_id = ledger.approved['id'] if ledger.approved else None
    return dict(forecast=_public_forecast(snapshot),
                policy=ledger.policy, approved=_card(ledger.approved, snapshot, approved_id),
                proposals=[_card(item, snapshot, approved_id) for item in reversed(ledger.proposals.values())])


def _text(payload, key, limit=3500):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'Заполните поле {key} (до {limit} знаков).')
    return value.strip()


def _policy(ledger, payload, common):
    if payload.get('policy_version') != ledger.policy['version'] or type(payload.get('policy_version')) is not int:
        raise ValueError('Политика изменилась. Обновите страницу перед сохранением.')
    mode = payload.get('mode')
    if mode not in ('manual', 'auto'):
        raise ValueError('Неизвестный режим согласования.')
    limit = until = None
    if mode == 'auto':
        limit = hours_value(str(payload.get('max_delta_hours', '')), 'Предел изменения')
        if limit is None:
            raise ValueError('Задайте предел изменения в чел·ч.')
        try:
            until = date.fromisoformat(_text(payload, 'valid_until', 10)).isoformat()
        except ValueError:
            raise ValueError('Срок полномочия — дата в формате ГГГГ-ММ-ДД.') from None
        if until < date.today().isoformat():
            raise ValueError('Срок полномочия уже истёк.')
        if ledger.approved is None:
            raise ValueError('Сначала РП утверждает исходную оценку этапа.')
    return dict(common, mode=mode, version=ledger.policy['version'] + 1,
                kind='effort', max_delta_hours=float(limit) if limit is not None else None,
                exact_limit=str(limit) if limit is not None else None, valid_until=until,
                baseline_id=ledger.approved['id'] if ledger.approved else None)


def _hold_reason(db, stage, forecast):
    if forecast['issues']:
        return forecast['issues'][0]
    if forecast['total_hours'] is None:
        return 'Для утверждения этапа нужны полные оценки всех фич.'
    blockers = blocking_threads(db, stage)
    if blockers:
        question = blockers[0]
        return 'Блокирующий вопрос: ' + (question['subject'] or question['messages'][0]['text'])
    return ''


def _proposal(db, stage, ledger, forecast, payload, common):
    if payload.get('fingerprint') != forecast['fingerprint']:
        raise ValueError('Исходные документы изменились. Пересчитайте предложение.')
    replaces = payload.get('replaces') or None
    if replaces is not None and (not isinstance(replaces, str) or replaces not in ledger.proposals):
        raise ValueError('Предыдущее предложение отсутствует в этом этапе.')
    current = ledger.approved
    previous = Fraction(current['snapshot']['exact_hours']) if current else None
    expected = Fraction(forecast['exact_hours']) if forecast['exact_hours'] is not None else None
    reason = _hold_reason(db, stage, forecast)
    policy = ledger.policy
    if not reason and policy['mode'] == 'auto':
        baseline = ledger.proposals[policy['baseline_id']]['snapshot']
        if policy['valid_until'] < date.today().isoformat():
            reason = 'Истёк срок полномочия.'
        elif baseline['scope_fingerprint'] != forecast['scope_fingerprint']:
            reason = 'Изменились границы, риски или условия согласованного объёма.'
        elif abs(expected - Fraction(baseline['exact_hours'])) > Fraction(policy['exact_limit']):
            reason = 'Изменение относительно основы полномочия превысило предел РП.'
    elif not reason:
        reason = 'Ожидает решения РП.'
    accepted = not reason
    return dict(common, id=common['request_id'], status='accepted' if accepted else 'pending', replaces=replaces,
                policy_version=policy['version'], policy_author=policy['author'], snapshot=forecast,
                base_id=current['id'] if current else None, stage_revision=forecast['stage_revision'],
                known_hours=forecast['known_hours'], total_hours=forecast['total_hours'], previous_hours=_hours(previous),
                delta_hours=_hours(expected - previous) if previous is not None and expected is not None else None,
                decided_at=common['created_at'] if accepted else None, decided_by='Чиф' if accepted else None,
                decision_reason=reason or 'Согласованное изменение в пределах полномочия.', method='auto' if accepted else None)


def _resolve(db, stage, ledger, forecast, payload, common):
    key = _text(payload, 'proposal', 36)
    old = ledger.proposals.get(key)
    if not old or old['status'] != 'pending':
        raise ValueError('Для решения выберите открытое предложение этого этапа.')
    if payload['action'] == 'accept':
        card = _card(old, forecast, ledger.approved['id'] if ledger.approved else None)
        if card['stale']:
            raise ValueError('Предложение устарело. Пересчитайте его. Изменились: ' + ', '.join(card['changed_inputs']))
        reason = _hold_reason(db, stage, forecast)
        if reason:
            raise ValueError(reason)
    return dict(old, status='accepted' if payload['action'] == 'accept' else 'rejected',
                method='manual', decided_at=common['created_at'], decided_by=common['author'],
                decision_role=common['role'], decision_reason=common['reason'])


def decide(vault, stage: str, payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get('project_id') != vault.project_id:
        raise ValueError('Команда относится к другому проекту.')
    request_id = str(UUID(_text(payload, 'request_id', 36)))
    action = payload.get('action')
    role = payload.get('role')
    if role not in ('manager', 'analyst', 'engineer'):
        raise ValueError('Неизвестная роль.')
    if action not in ('policy', 'propose', 'accept', 'reject'):
        raise ValueError('Неизвестная операция переоценки.')
    if action != 'propose' and role != 'manager':
        raise ValueError('Полномочия и итоговое решение записывает РП.')
    common = dict(request_id=request_id, author=_text(payload, 'author', 120), role=role,
                  reason=_text(payload, 'reason'), created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'))
    with vault.locked() as db:
        db.execute('CREATE TABLE IF NOT EXISTS stage_decision_events (request_id TEXT PRIMARY KEY, stage TEXT NOT NULL, '
                   'action TEXT NOT NULL, input_hash TEXT NOT NULL, document TEXT NOT NULL)')
        receipt = db.execute('SELECT * FROM stage_decision_events WHERE request_id=?', (request_id,)).fetchone()
        if receipt:
            if receipt['stage'] != stage or receipt['input_hash'] != _hash(payload):
                raise ValueError('ID операции уже использован с другими параметрами.')
            item = json.loads(receipt['document'])
        else:
            forecast = _snapshot(vault, db, stage)
            ledger = Ledger.read(db, stage)
            if action == 'policy':
                item = _policy(ledger, payload, common)
            elif action == 'propose':
                item = _proposal(db, stage, ledger, forecast, payload, common)
            else:
                item = _resolve(db, stage, ledger, forecast, payload, common)
            db.execute('INSERT INTO stage_decision_events VALUES (?, ?, ?, ?, ?)',
                       (request_id, stage, action, _hash(payload), _json(item)))
    return {k: v for k, v in item.items() if k != 'snapshot'}
