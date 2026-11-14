"""Снимки плана этапа и записи учёта с раздельными датами события и публикации."""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from uuid import UUID, uuid4

from .stage_resources import ActualTotals, calculate_resources, evaluate_plan_at


_COLUMNS = {
    'teams': {'role', 'people', 'hours_per_day', 'allocation', 'focus', 'rate'},
    'dependencies': {'work', 'after', 'wait_days'},
    'assignments': {'work', 'role'},
    'api': {'model', 'input_tokens', 'output_tokens', 'input_rate', 'output_rate'},
}
_PLAN_FIELDS = {'source', 'start', 'target', 'weekdays', 'holidays', 'currency', 'extra_cost', *_COLUMNS}
_ACTUAL_FIELDS = {'source', 'date', 'role', 'hours', 'cost', 'currency'}
_BASELINE_FIELDS = ('total_hours', 'known_hours', 'finish', 'cost', 'known_cost', 'currency')
_ACTUAL_METRICS = ('actual_hours', 'known_actual_hours', 'actual_cost', 'known_actual_cost', 'actual_currency')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _require_stage(db, stage):
    if not isinstance(stage, str) or not db.execute(
            "SELECT 1 FROM records WHERE key=? AND kind='stage'", (stage,)).fetchone():
        raise ValueError('Выберите этап этого проекта.')


def _text(value, field, limit=1500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{field} — нужен текст длиной до {limit} знаков.')
    return value.strip()


def _observed_day(value):
    try:
        day = date.fromisoformat(value)
        if value != day.isoformat():
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError('Дата наблюдения — календарная дата в формате ГГГГ-ММ-ДД.') from None
    if day > date.today():
        raise ValueError('Дата наблюдения ещё не наступила. Будущий срок укажите в плане.')
    return value


def _fields(record, allowed, name):
    if not isinstance(record, dict):
        raise ValueError(f'{name} — требуется запись с полями.')
    unknown = record.keys() - allowed
    if unknown:
        raise ValueError(f'{name} — неизвестные поля: {", ".join(sorted(unknown))}.')


def _entry(action, payload, forecast):
    """Сохраняем исходные десятичные значения. Смысл единиц проверяет сам расчёт."""
    if action == 'checkpoint':
        return _observed_day(payload.get('date')), _text(payload.get('source'), 'Основание сверки'), None
    value = payload.get(action)
    if action == 'actual':
        _fields(value, _ACTUAL_FIELDS, 'Факт')
        if all(value.get(key) in (None, '') for key in ('hours', 'cost')):
            raise ValueError('Укажите хотя бы фактические часы или фактическую стоимость.')
        return _observed_day(value.get('date')), _text(value.get('source'), 'Источник факта'), value

    _fields(value, _PLAN_FIELDS, 'План')
    for table, fields in _COLUMNS.items():
        rows = value.get(table, [])
        if not isinstance(rows, list):
            raise ValueError(f'{table} — ожидается список строк плана.')
        for row in rows:
            _fields(row, fields, table)
    keys = {row['key'] for row in forecast.get('resource_work', [])}
    for table, references in (('assignments', ('work',)), ('dependencies', ('work', 'after'))):
        for row in value.get(table, []):
            for field in references:
                key = _text(row.get(field), 'ID работы', 350)
                if key not in keys:
                    raise ValueError(f'Работа «{key}» отсутствует в текущем составе этапа.')
    return _observed_day(payload.get('date')), _text(value.get('source'), 'Основание плана'), value


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS stage_resource_events (
        stage TEXT NOT NULL, version INTEGER NOT NULL, request_id TEXT NOT NULL UNIQUE,
        input_hash TEXT NOT NULL, receipt TEXT NOT NULL, entry TEXT, forecast TEXT NOT NULL,
        schedule TEXT, PRIMARY KEY(stage, version))''')


def _events(db, stage, version=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_resource_events'").fetchone():
        return []
    query = 'SELECT receipt,entry,schedule FROM stage_resource_events WHERE stage=?'
    params = [stage]
    if version is not None:
        query += ' AND version<=?'
        params.append(version)
    rows = db.execute(query + ' ORDER BY version', params)
    return [dict(json.loads(row['receipt']), entry=json.loads(row['entry']) if row['entry'] else None,
                 schedule=json.loads(row['schedule']) if row['schedule'] else None) for row in rows]


def _effective_plan(events, day):
    plans = (e for e in events if e['action'] == 'plan' and e['date'] <= day)
    return max(plans, key=lambda e: (e['date'], e['version']), default=None)


def _actuals(events, day):
    return [e['entry'] for e in events if e['action'] == 'actual' and e['date'] <= day]


def _baseline(plan_event):
    if not plan_event:
        return None
    return dict(version=plan_event['version'], date=plan_event['date'],
                **{k: plan_event['metrics'][k] for k in _BASELINE_FIELDS})


def _trend(events):
    """Факт относится к дню выполнения. В квитанции остаётся то, что было известно при записи."""
    history = []
    baseline = schedule = last_forecast = None
    totals = ActualTotals()
    for event in sorted(events, key=lambda e: (e['date'], e['version'])):
        action = event['action']
        if action == 'plan':
            baseline = _baseline(event)
            schedule = event['schedule']
        if action == 'actual':
            totals.add(event['entry'])
        else:
            last_forecast = event['metrics']
        # При позднем табеле текущая оценка из Obsidian не становится прогнозом вчерашнего дня.
        metrics = dict(last_forecast) if last_forecast else {k: None for k in _BASELINE_FIELDS}
        totals.currency = (baseline or {}).get('currency') or ''
        metrics.update(totals.summary())
        metrics.update(evaluate_plan_at(schedule, event['date']))
        public = {k: v for k, v in event.items() if k not in ('schedule', 'entry', 'metrics')}
        history.append(dict(public, metrics=metrics, baseline=baseline))
    return history


def tracked_stages(vault):
    """Наблюдаем только за этапами, у которых уже есть журнал ресурсов."""
    with vault.reading() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_resource_events'").fetchone()
        if not table:
            return []
        return [row[0] for row in db.execute('SELECT DISTINCT stage FROM stage_resource_events ORDER BY stage')]


def _resource_basis(forecast):
    fields = ('total_hours', 'known_hours', 'complete_count', 'feature_count', 'base_hours', 'reserve_hours',
              'resource_work', 'shared_reserves', 'issues')
    coverage = [{key: item.get(key) for key in ('key', 'complete', 'issues')} for item in forecast.get('features', [])]
    return {**{key: forecast.get(key) for key in fields}, 'features': coverage}


def observe_forecast(vault, stage, forecast_callable):
    """Публикует изменение оценки один раз, даже если за этапом следят два процесса."""
    with vault.locked() as db:
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage_resource_events'").fetchone()
        if not table:
            return None
        previous = db.execute("""SELECT forecast FROM stage_resource_events WHERE stage=?
                                 AND json_extract(receipt,'$.action') IN ('checkpoint','plan')
                                 ORDER BY version DESC LIMIT 1""", (stage,)).fetchone()
        if not previous and not db.execute('SELECT 1 FROM stage_resource_events WHERE stage=? LIMIT 1', (stage,)).fetchone():
            return None
        estimate = forecast_callable(vault, db, stage)
        # Поздний табель может прийти между правкой O/M/P и проходом наблюдателя.
        # Дата табеля не может заменить момент, в который обнаружился новый прогноз.
        if previous and _resource_basis(json.loads(previous['forecast'])) == _resource_basis(estimate):
            return None
        day = date.today().isoformat()
        return _append_event(db, stage, _events(db, stage), estimate, action='checkpoint', entry=None, day=day,
                             source='Изменение оценки в документах этапа', author='Чиф', role='manager',
                             request_id=str(uuid4()), signature='observer')


def resource_view(vault, stage, forecast):
    """Открытие монитора сохраняет режим чтения даже у старой базы без таблицы ресурсов."""
    with vault.reading() as db:
        _require_stage(db, stage)
        events = _events(db, stage)
    today = date.today().isoformat()
    baseline = _effective_plan(events, today)
    plan = baseline['entry'] if baseline else {}
    actuals = _actuals(events, today)
    current = calculate_resources(forecast, plan, actuals, as_of=today)
    # При изменении оценки сравниваем факт с последним планом, замороженным РП.
    current.update(evaluate_plan_at(baseline['schedule'] if baseline else None, today))
    current.pop('schedule', None)
    return dict(method=current['method'], version=events[-1]['version'] if events else 0,
                fingerprint=forecast['fingerprint'], as_of=today, plan=plan, actuals=actuals,
                history=_trend(events), baseline=_baseline(baseline), current=current)


def resource_snapshot(vault, stage, version):
    """Воспроизводимые входы среза без сведений, внесённых после его публикации."""
    if type(version) is not int or version < 1:
        raise ValueError('Укажите номер сохранённого среза.')
    with vault.reading() as db:
        _require_stage(db, stage)
        events = _events(db, stage, version)
        if not events or events[-1]['version'] != version:
            raise FileNotFoundError('Срез с таким номером отсутствует в этом этапе.')
        row = db.execute('SELECT receipt,forecast FROM stage_resource_events WHERE stage=? AND version=?',
                         (stage, version)).fetchone()
        receipt = json.loads(row['receipt'])
        plan = _effective_plan(events, receipt['date'])
        baseline = _baseline(plan)
        if baseline:
            # В срезе после переоценки календарь плана основан на прежней трудоёмкости работ.
            saved = db.execute('SELECT forecast FROM stage_resource_events WHERE stage=? AND version=?',
                               (stage, baseline['version'])).fetchone()
            baseline['forecast'] = json.loads(saved['forecast'])
            baseline['schedule'] = plan['schedule']
    return dict(method=receipt['method'], receipt=receipt, forecast=json.loads(row['forecast']),
                plan=plan['entry'] if plan else {}, baseline=baseline, actuals=_actuals(events, receipt['date']))


def record_resource(vault, stage, forecast, payload):
    """Закрепляет исходные данные и сумму одной версией под блокировкой проектной базы."""
    if not isinstance(payload, dict) or payload.get('project_id') != vault.project_id or payload.get('stage', stage) != stage:
        raise ValueError('Команда относится к другому проекту или этапу.')
    action = payload.get('action')
    if action not in ('plan', 'checkpoint', 'actual'):
        raise ValueError('Неизвестная запись ресурсов этапа.')
    role = payload.get('role', 'manager')
    if role not in ('manager', 'analyst', 'engineer') or action != 'actual' and role != 'manager':
        raise ValueError('План и контрольный срез фиксирует РП.')
    author = _text(payload.get('author'), 'Автор записи', 120)
    request_id = str(UUID(_text(payload.get('request_id'), 'ID операции', 36)))
    expected = payload.get('expected_version')
    if type(expected) is not int or expected < 0:
        raise ValueError('Укажите версию открытого журнала ресурсов.')
    signature = hashlib.sha256(_json(payload).encode()).hexdigest()
    with vault.locked() as db:
        _require_stage(db, stage)
        _schema(db)
        previous = db.execute('SELECT stage,input_hash,receipt FROM stage_resource_events WHERE request_id=?',
                              (request_id,)).fetchone()
        if previous:
            if previous['stage'] != stage or previous['input_hash'] != signature:
                raise ValueError('ID операции уже использован с другими параметрами.')
            return json.loads(previous['receipt'])
        events = _events(db, stage)
        version = events[-1]['version'] if events else 0
        if expected != version:
            raise ValueError('Журнал ресурсов изменился. Сверьте новые записи перед сохранением.')
        estimate = forecast(vault, db, stage) if callable(forecast) else forecast
        if not estimate.get('fingerprint') or payload.get('fingerprint') != estimate['fingerprint']:
            raise ValueError('Исходная оценка этапа изменилась. Обновите основу записи.')
        day, source, entry = _entry(action, payload, estimate)
        return _append_event(db, stage, events, estimate, action=action, entry=entry, day=day,
                             source=source, author=author, role=role, request_id=request_id, signature=signature)


def _append_event(db, stage, events, estimate, *, action, entry, day, source, author, role, request_id, signature):
    version = events[-1]['version'] + 1 if events else 1
    plan_event = _effective_plan(events, day)
    plan = entry if action == 'plan' else (plan_event['entry'] if plan_event else {})
    actuals = _actuals(events, day)
    if action == 'actual':
        actuals.append(entry)
    calculation = calculate_resources(estimate, plan, actuals, as_of=day)
    schedule = calculation['schedule']
    metrics = {**{k: calculation[k] for k in (*_BASELINE_FIELDS, *_ACTUAL_METRICS)},
               'complete_count': estimate.get('complete_count'), 'feature_count': estimate.get('feature_count')}
    baseline_schedule = schedule if action == 'plan' else plan_event and plan_event['schedule']
    metrics.update(evaluate_plan_at(baseline_schedule, day))
    receipt = dict(version=version, request_id=request_id, action=action, date=day, source=source,
                   author=author, role=role, method=calculation['method'], fingerprint=estimate['fingerprint'],
                   created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), metrics=metrics)
    db.execute('INSERT INTO stage_resource_events VALUES (?,?,?,?,?,?,?,?)',
               (stage, version, request_id, signature, _json(receipt), _json(entry) if entry else None,
                _json(estimate), _json(schedule) if schedule and action == 'plan' else None))
    return receipt
