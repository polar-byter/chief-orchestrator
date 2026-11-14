"""Ресурсы этапа. Часы, рабочие дни и деньги рассчитываются в своих единицах."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date
from fractions import Fraction
from heapq import heappop, heappush
import re


METHOD = 'stage-resources-v1'
ZERO = Fraction()
MAX_ROWS = 10_000


def _number(value, field, *, maximum=None, integer=False, ratio=False):
    if value is None or value == '':
        return None
    raw = str(value).strip().replace(',', '.')
    pattern = r'\d+(?:\.\d+)?' if not ratio else r'\d+(?:[./]\d+)?'
    if len(raw) > 50 or not re.fullmatch(pattern, raw):
        raise ValueError(f'{field} — требуется конечное неотрицательное число.')
    try:
        number = Fraction(raw)
    except ZeroDivisionError as error:
        raise ValueError(f'{field} — знаменатель не может быть нулём.') from error
    if integer and number.denominator != 1:
        raise ValueError(f'{field} — укажите целое число.')
    if maximum is not None and number > maximum:
        raise ValueError(f'{field} — допустимо значение до {maximum}.')
    return number


def _display(value, digits=1):
    """Округляет половину вверх один раз, после суммирования точных долей."""
    if value is None:
        return None
    scale = 10 ** digits
    return int(value * scale + Fraction(1, 2)) / scale


def _date(value, field):
    if value in (None, ''):
        return None
    if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f'{field} — требуется существующая дата в формате ГГГГ-ММ-ДД.')


def _label(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 350:
        raise ValueError(f'{field} — укажите название длиной до 350 символов.')
    return value.strip()


def _currency(value):
    if value in ('', None):
        return ''
    if not isinstance(value, str) or not re.fullmatch('[A-Z]{3}', value):
        raise ValueError('Укажите трёхбуквенный код валюты, например RUB.')
    return value


def _rows(data, field):
    rows = data.get(field, [])
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(r, dict) for r in rows):
        raise ValueError(f'{field} — нужен список записей, не более {MAX_ROWS}.')
    return rows


class ActualTotals:
    """Накопленные записи учёта. Нулевая запись отличается от отсутствующей."""

    def __init__(self, currency=''):
        self.currency = _currency(currency)
        self.count = self.hour_count = 0
        self.hours = ZERO
        self.money = defaultdict(Fraction)
        self.money_count = defaultdict(int)

    def add(self, row):
        hours = _number(row.get('hours'), 'Фактический труд')
        cost = _number(row.get('cost'), 'Фактическая стоимость')
        currency = _currency(row.get('currency'))
        if cost is not None and not currency:
            raise ValueError('Для фактической суммы нужна валюта записи.')
        self.count += 1
        if hours is not None:
            self.hours += hours
            self.hour_count += 1
        if cost is not None:
            self.money[currency] += cost
            self.money_count[currency] += 1

    def summary(self):
        currency = self.currency or (next(iter(self.money)) if len(self.money) == 1 else '')
        known_cost = self.money.get(currency)
        return dict(actual_hours=_display(self.hours) if self.count and self.hour_count == self.count else None,
                    known_actual_hours=_display(self.hours) if self.hour_count else None,
                    actual_cost=_display(known_cost, 2) if self.count and self.money_count.get(currency) == self.count else None,
                    known_actual_cost=_display(known_cost, 2), actual_currency=currency)


class WorkingCalendar:
    def __init__(self, weekdays, holidays):
        if not isinstance(weekdays, list) or any(type(d) is not int or not 1 <= d <= 7 for d in weekdays):
            raise ValueError('Рабочие дни — числа от 1 (понедельник) до 7 (воскресенье).')
        if not isinstance(holidays, list) or len(holidays) > MAX_ROWS:
            raise ValueError(f'Календарь допускает до {MAX_ROWS} исключений.')
        self.weekdays = set(weekdays)
        exclusions = {_date(h, 'Нерабочая дата') for h in holidays}
        if None in exclusions:
            raise ValueError('Пустая нерабочая дата в календаре.')
        self.holidays = sorted(d for d in exclusions if d.isoweekday() in self.weekdays)

    def count(self, start, end):
        if end < start:
            return 0
        weeks, remainder = divmod((end - start).days + 1, 7)
        extra = sum((start.weekday() + d) % 7 + 1 in self.weekdays for d in range(remainder))
        omitted = bisect_right(self.holidays, end) - bisect_left(self.holidays, start)
        return weeks * len(self.weekdays) + extra - omitted

    def date_at(self, start, days):
        """При точном окончании рабочего дня результатом остаётся дата этого дня."""
        needed = max(1, -(-days.numerator // days.denominator))
        if not self.weekdays or self.count(start, date.max) < needed:
            return None
        left, right = start.toordinal(), date.max.toordinal()
        while left < right:
            middle = (left + right) // 2
            if self.count(start, date.fromordinal(middle)) < needed:
                left = middle + 1
            else:
                right = middle
        return date.fromordinal(left)


def _teams(plan):
    result = {}
    for row in _rows(plan, 'teams'):
        role = _label(row.get('role'), 'Команда')
        if role in result:
            raise ValueError(f'{role} — повторная строка команды.')
        people = _number(row.get('people'), f'{role}, численность', maximum=1_000_000, integer=True)
        day = _number(row.get('hours_per_day'), f'{role}, часов в день', maximum=24)
        allocation = _number(row.get('allocation'), f'{role}, доля участия', maximum=1)
        focus = _number(row.get('focus'), f'{role}, фокус-фактор', maximum=1)
        rate = _number(row.get('rate'), f'{role}, часовая ставка')
        daily = None if None in (people, day, allocation, focus) else people * day * allocation * focus
        result[role] = dict(daily=daily, rate=rate)
    return result


def _work(forecast, plan, issues):
    assignments = {}
    for entry in _rows(plan, 'assignments'):
        key = _label(entry.get('work'), 'Работа в назначении')
        if key in assignments:
            raise ValueError(f'{key} — оставьте одно назначение команды.')
        assignments[key] = _label(entry.get('role'), f'{key}, команда')
    work = {}
    for row in _rows(forecast, 'resource_work'):
        key = _label(row.get('key'), 'ID работы')
        if key in work:
            raise ValueError(f'{key} — повторный ID работы.')
        role = assignments.get(key) or (row.get('role') or '').strip()
        work[key] = (role, _number(row.get('hours'), f'{key}, труд', ratio=True))
    for key in sorted(assignments.keys() - work.keys()):
        issues.append(f'Назначение {key} ссылается на работу из прежнего состава этапа.')
    return work


def _dependencies(plan, work, issues):
    following = defaultdict(list)
    parents = dict.fromkeys(work, 0)
    edges = set()
    complete = True
    for row in _rows(plan, 'dependencies'):
        child = _label(row.get('work'), 'Зависимая работа')
        parent = _label(row.get('after'), 'Предшественник')
        lag = _number(row.get('wait_days', 0), 'Ожидание в рабочих днях', integer=True, maximum=1_000_000)
        if lag is None:
            complete = False
            issues.append(f'{parent} → {child} — уточните длительность ожидания.')
        if (parent, child) in edges:
            raise ValueError(f'{parent} → {child} — повторная зависимость.')
        edges.add((parent, child))
        if parent not in work or child not in work:
            complete = False
            issues.append(f'{parent} → {child} — сверьте зависимость с составом этапа.')
            continue
        following[parent].append((child, lag or ZERO))
        parents[child] += 1
    # Цикл обнаруживается даже если календарь или число часов ещё не заполнены.
    remaining = parents.copy()
    ready = [key for key, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        key = ready.pop()
        visited += 1
        for child, _ in following[key]:
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if visited != len(work):
        raise ValueError('В зависимостях работ есть цикл. Проверьте предшественников.')
    return following, parents, complete


def _schedule(work, teams, following, parents):
    """Доступная первой команда берёт готовую работу целиком. При равенстве выбирается ID.

    Все участники роли образуют общий фонд часов. Разные роли могут работать
    одновременно. Окончание и ожидание измеряются долями продуктивного дня.
    """
    by_role = defaultdict(list)
    available, released, version = defaultdict(Fraction), defaultdict(Fraction), defaultdict(int)
    starts, slots = [], []

    def offer(role):
        version[role] += 1
        if by_role[role]:
            release, key = by_role[role][0]
            heappush(starts, (max(available[role], release), key, role, version[role]))

    def enqueue(key):
        role = work[key][0]
        heappush(by_role[role], (released[key], key))
        offer(role)

    for key in work:
        if not parents[key]:
            enqueue(key)
    while starts:
        begin, key, role, ticket = heappop(starts)
        if ticket != version[role]:
            continue
        heappop(by_role[role])
        _, hours = work[key]
        team = teams.get(role, {})
        end = begin + (hours / team['daily'] if hours else ZERO)
        available[role] = end
        offer(role)
        slots.append(dict(work=key, role=role, begin=str(begin), end=str(end), hours=str(hours),
                          rate=str(team['rate']) if team.get('rate') is not None else None))
        for child, lag in following[key]:
            released[child] = max(released[child], end + lag)
            parents[child] -= 1
            if not parents[child]:
                enqueue(child)
    return slots


def evaluate_plan_at(schedule, as_of):
    result = dict(planned_hours_to_date=None, planned_labor_cost_to_date=None)
    day = _date(as_of, 'Дата сверки')
    if not schedule or not day:
        return result
    calendar = WorkingCalendar(schedule['weekdays'], schedule['holidays'])
    elapsed = calendar.count(date.fromisoformat(schedule['start']), day)
    hours = money = ZERO
    cost_known = bool(schedule.get('currency'))
    for slot in schedule['slots']:
        begin, end = Fraction(slot['begin']), Fraction(slot['end'])
        if elapsed <= begin or end == begin:
            continue
        done = Fraction(slot['hours']) * (min(Fraction(elapsed), end) - begin) / (end - begin)
        hours += done
        if slot['rate'] is None:
            cost_known = False
        else:
            money += done * Fraction(slot['rate'])
    return dict(planned_hours_to_date=_display(hours), planned_labor_cost_to_date=_display(money, 2) if cost_known else None)


def _api_cost(plan, issues):
    known = ZERO
    complete = True
    priced = False
    for row in _rows(plan, 'api'):
        model = _label(row.get('model'), 'Модель API')
        for side, caption in (('input', 'входа'), ('output', 'выхода')):
            tokens = _number(row.get(side + '_tokens'), f'{model}, токены {caption}', integer=True)
            rate = _number(row.get(side + '_rate'), f'{model}, тариф {caption}')
            if tokens is None or (tokens and rate is None):
                issues.append(f'{model} — уточните количество токенов и тариф {caption}.')
                complete = False
            else:
                priced = True
                if tokens:
                    known += tokens * rate / 1_000_000
    return known, complete, priced


def calculate_resources(forecast: dict, plan: dict, actuals: list[dict], as_of='') -> dict:
    issues = []
    teams = _teams(plan)
    work = _work(forecast, plan, issues)
    following, parents, links_complete = _dependencies(plan, work, issues)
    calendar = WorkingCalendar(plan.get('weekdays', []), plan.get('holidays', []))
    start, target = _date(plan.get('start'), 'Начало этапа'), _date(plan.get('target'), 'Конец периода загрузки')
    if start and target and target < start:
        raise ValueError('Конец периода загрузки раньше начала этапа.')
    days = calendar.count(start, target) if start and target and calendar.weekdays else None
    currency = _currency(plan.get('currency'))
    totals = ActualTotals(currency)
    actual_by_role = defaultdict(lambda: ActualTotals(currency))
    observed = _date(as_of, 'Дата сверки')
    for row in actuals:
        day = _date(row.get('date'), 'Дата факта')
        if not day:
            raise ValueError('Укажите дату факта.')
        if observed and day > observed:
            continue
        role = _label(row.get('role'), 'Команда в факте')
        totals.add(row)
        actual_by_role[role].add(row)

    demand = defaultdict(Fraction)
    unknown_roles = set()
    known_work_roles = set()
    role_missing = False
    for key, (role, hours) in work.items():
        if hours == 0:
            if role:
                known_work_roles.add(role)
            continue
        if not role:
            role_missing = True
            issues.append(f'{key} — назначьте команду для распределения труда.')
        if hours is None:
            unknown_roles.add(role)
        elif role:
            demand[role] += hours
            known_work_roles.add(role)

    complete = forecast.get('total_hours') is not None and bool(work)
    if not complete:
        issues.append('Полная оценка труда этапа пока неизвестна.')
    scope_known = complete and not role_missing and not unknown_roles
    calendar_ready = bool(start and calendar.weekdays)
    if not calendar_ready:
        issues.append('Для прогноза срока нужны дата начала и рабочие дни.')
    if not target:
        issues.append('Для загрузки команд укажите конец периода.')
    labor = ZERO
    priced_labor = False
    rates_known = bool(currency)
    capacities_known = True
    team_rows = []
    for role in sorted(teams.keys() | known_work_roles | unknown_roles | actual_by_role.keys()):
        if not role:
            continue
        team = teams.get(role, {})
        daily, rate = team.get('daily'), team.get('rate')
        required = demand.get(role, ZERO)
        if required and not daily:
            capacities_known = False
            issues.append(f'{role} — для срока нужна положительная дневная ёмкость.')
        if required and rate is None:
            rates_known = False
            issues.append(f'{role} — укажите часовую ставку.')
        if rate is not None and role in known_work_roles:
            priced_labor = True
            labor += required * rate
        capacity = daily * days if daily is not None and days is not None else None
        known = required if scope_known or role in known_work_roles else None
        load = known * 100 / capacity if known is not None and capacity else None
        actual = actual_by_role[role].summary()
        team_rows.append(dict(role=role, demand_hours=_display(required) if scope_known else None,
                              known_demand_hours=_display(known), daily_hours=_display(daily),
                              capacity_hours=_display(capacity), load_percent=_display(load) if scope_known else None,
                              known_load_percent=_display(load), actual_hours=actual['actual_hours'],
                              known_actual_hours=actual['known_actual_hours']))

    finish = schedule = None
    if calendar_ready and capacities_known and scope_known and links_complete:
        slots = _schedule(work, teams, following, parents)
        finish = calendar.date_at(start, max(Fraction(slot['end']) for slot in slots))
        if finish is None:
            issues.append('Прогноз выходит за пределы поддерживаемых календарных дат.')
        schedule = dict(start=start.isoformat(), weekdays=sorted(calendar.weekdays),
                        holidays=[day.isoformat() for day in calendar.holidays], slots=slots, currency=currency)
    extra = _number(plan.get('extra_cost'), 'Прочие расходы')
    api, api_known, priced_api = _api_cost(plan, issues)
    if extra is None or not currency:
        issues.append('Для полной стоимости задайте валюту и прочие расходы (допустим ноль).')
    priced = bool(currency) and (extra is not None or priced_labor or priced_api or scope_known and rates_known)
    known_cost = labor + api + (extra or ZERO)
    cost = known_cost if scope_known and rates_known and api_known and extra is not None else None
    actual = totals.summary()
    if totals.money and (len(totals.money) > 1 or currency and currency not in totals.money):
        issues.append('Фактические суммы в разных валютах учитываются раздельно.')
    return dict(method=METHOD, total_hours=forecast.get('total_hours'), known_hours=forecast.get('known_hours'),
                finish=finish.isoformat() if finish else None, working_days=days, currency=currency,
                cost=_display(cost, 2), known_cost=_display(known_cost, 2) if priced else None,
                planned_labor_cost=_display(labor, 2) if scope_known and rates_known else None,
                api_cost=_display(api, 2) if api_known and currency else None,
                extra_cost=_display(extra, 2) if currency else None, teams=team_rows, issues=issues, schedule=schedule,
                **actual, **evaluate_plan_at(schedule, as_of))
