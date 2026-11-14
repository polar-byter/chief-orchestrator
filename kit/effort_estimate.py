"""Трёхточечная оценка труда и однократный учёт рисков одной фичи."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import re


@dataclass(frozen=True)
class WorkEstimate:
    key: str
    role: str
    unit: str
    optimistic: str
    likely: str
    pessimistic: str
    basis: str
    included_risks: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskAllowance:
    key: str
    works: tuple[str, ...]
    treatment: str
    hours: str


@dataclass(frozen=True)
class EstimatedWork:
    work: WorkEstimate
    mean: Fraction | None
    deviation: Fraction | None


@dataclass(frozen=True)
class Estimate:
    rows: list[EstimatedWork]
    reserves: dict[str, Fraction | None]
    missing: list[str]

    @property
    def base(self) -> Fraction:
        return sum((row.mean for row in self.rows if row.mean is not None), Fraction())

    @property
    def reserve(self) -> Fraction:
        return sum((hours for hours in self.reserves.values() if hours is not None), Fraction())

    @property
    def total(self) -> Fraction | None:
        return None if self.missing else self.base + self.reserve


def hours_value(value: str, field: str) -> Fraction | None:
    value = value.strip().casefold()
    if value in ('', '—', 'нет данных', 'неизвестно'):
        return None
    if not re.fullmatch(r'\d+(?:[.,]\d+)?', value):
        raise ValueError(f'{field} — укажите конечное неотрицательное число часов.')
    return Fraction(value.replace(',', '.'))


def display_hours(value: Fraction | None) -> str:
    if value is None:
        return 'Нет данных'
    # Округляем половину вверх только при выводе; суммы сохраняют точные дроби.
    tenths = int(value * 10 + Fraction(1, 2))
    return f'{tenths // 10},{tenths % 10}'


def estimate_effort(works: list[WorkEstimate], risks: list[RiskAllowance]) -> Estimate:
    """Возвращает сумму по известным строкам и основания, мешающие получить полный итог."""
    rows, reserves, missing = [], {}, []
    work_ids: set[str] = set()
    covered: dict[str, set[str]] = {}
    for work in works:
        if not work.key or work.key in work_ids:
            raise ValueError(f'Работа «{work.key}» — требуется уникальный ID.')
        work_ids.add(work.key)
        if work.unit != 'чел·ч':
            raise ValueError(f'{work.key} — для O/M/P нужна единица чел·ч.')
        for risk_id in work.included_risks:
            covered.setdefault(risk_id, set()).add(work.key)
        o, m, p = (hours_value(value, f'{work.key}, {field}') for field, value in
                   zip(('O', 'M', 'P'), (work.optimistic, work.likely, work.pessimistic)))
        if ((o is not None and m is not None and o > m) or
                (m is not None and p is not None and m > p)):
            raise ValueError(f'{work.key}, M — должно выполняться O ≤ M ≤ P.')
        if o is not None and p is not None and o > p:
            raise ValueError(f'{work.key}, P — меньше O.')
        if o is None or m is None or p is None:
            missing.append(f'{work.key} — заполните O, M и P.')
            rows.append(EstimatedWork(work, None, None))
        else:
            rows.append(EstimatedWork(work, (o + 4 * m + p) / 6, (p - o) / 6))
    if not rows:
        missing.append('Введите первую работу и её O/M/P.')
    for key, owners in covered.items():
        if len(owners) > 1:
            raise ValueError(f'{key} включён в O/M/P нескольких работ: {", ".join(sorted(owners))}. '
                             'Выделите одну строку для общего последствия, остальные работы свяжите в реестре.')

    for risk in risks:
        if not risk.key or risk.key in reserves:
            raise ValueError(f'Риск «{risk.key}» — нужна одна строка на событие. Перечислите работы через запятую.')
        if not risk.works or set(risk.works) - work_ids:
            raise ValueError(f'{risk.key} — укажите существующие ID работ: {", ".join(risk.works) or "пусто"}.')
        counted = covered.get(risk.key, set())
        mode = risk.treatment.casefold()
        if counted and (mode != 'в o/m/p' or counted - set(risk.works)):
            raise ValueError(f'{risk.key} уже учтён в O/M/P работ {", ".join(sorted(counted))}. '
                             'Сверьте работы и способ учёта в реестре рисков.')
        hours = hours_value(risk.hours, f'{risk.key}, резерв')
        if mode == 'отдельно':
            reserves[risk.key] = hours
            if hours is None:
                missing.append(f'{risk.key} — РП должен указать отдельный резерв в чел·ч.')
        elif mode in ('в o/m/p', 'без резерва'):
            if hours:
                raise ValueError(f'{risk.key} — при решении «{risk.treatment}» отдельный резерв должен быть пустым или нулевым.')
            reserves[risk.key] = Fraction()
        elif mode in ('', '—', 'нет данных'):
            if hours is not None:
                raise ValueError(f'{risk.key} — для указанного резерва выберите способ учёта.')
            reserves[risk.key] = None
            missing.append(f'{risk.key} — выберите способ учёта риска.')
        else:
            raise ValueError(f'{risk.key} — допустимы «в O/M/P», «отдельно» и «без резерва».')
    for key in sorted(covered.keys() - reserves.keys()):
        missing.append(f'{key} включён в O/M/P. Добавьте решение РП в реестр рисков.')
    return Estimate(rows, reserves, missing)
