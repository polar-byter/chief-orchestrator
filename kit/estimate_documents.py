"""Таблицы рабочего места и отображение расчёта PERT в Obsidian."""
from __future__ import annotations

import re

from .effort_estimate import RiskAllowance, WorkEstimate, display_hours, estimate_effort


WORK_COLUMNS = ('Работа / ID', 'Роль', 'Единица', 'O', 'M', 'P', 'Источник и допущение')
RISK_COLUMN = 'Риски в O/M/P'
RISK_COLUMNS = ('ID риска', 'ID работ', 'Учёт', 'Резерв, чел·ч')
RESULT_NAME = 'Расчёт трудозатрат'
METHOD = 'PERT v1'


def input_table(columns: tuple[str, ...]) -> str:
    return '\n'.join('| ' + ' | '.join(row) + ' |' for row in (
        columns, ['---'] * len(columns), [''] * len(columns))) + '\n'


def setup_text(kind: str) -> str:
    if kind == 'effort':
        return ('\n\n## Работы для расчёта PERT\n\nИнженер указывает часы для одного объёма. '
                'Если последствие риска вошло в эти числа, укажите его ID в последней колонке.\n\n'
                + input_table(WORK_COLUMNS + (RISK_COLUMN,)))
    return ('\n\n## Распределение резерва\n\nРП выбирает «в O/M/P», «отдельно» или «без резерва». '
            'Одному событию соответствует один ID риска. Связанные работы перечислите через запятую. '
            'Для отдельного резерва укажите согласованные человеко-часы.\n\n' + input_table(RISK_COLUMNS))


def _cells(line: str) -> list[str]:
    cells = re.split(r'(?<!\\)\|', line.strip())
    if not cells[0]:
        cells = cells[1:]
    if cells and not cells[-1]:
        cells.pop()
    return [cell.strip().replace(r'\|', '|') for cell in cells]


def _table_lines(note: str, columns: tuple[str, ...]) -> list[list[str]] | None:
    """Читает одну таблицу с договорёнными колонками. Примеры внутри блоков кода пропускает."""
    found = None
    active = False
    fence = ''
    for line in note.splitlines():
        stripped = line.strip()
        delimiter = re.match(r'^(`{3,}|~{3,})', stripped)
        if delimiter:
            if not fence:
                fence = delimiter[1]
            elif stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = ''
            active = False
            continue
        if fence or '|' not in stripped:
            active = False
            continue
        cells = _cells(stripped)
        if len(cells) >= len(columns) and set(columns).issubset(cells) and tuple(cells[:len(columns)]) != columns:
            raise ValueError(f'В таблице «{columns[0]}» восстановите порядок колонок.')
        if tuple(cells[:len(columns)]) == columns:
            if found is not None:
                raise ValueError(f'Таблица «{columns[0]}» встречается дважды. Соберите строки в одной таблице.')
            if tuple(cells) not in (columns, WORK_COLUMNS + (RISK_COLUMN,)):
                raise ValueError(f'Проверьте названия колонок таблицы «{columns[0]}».')
            found, active, width = [], True, len(cells)
        elif active and not all(re.fullmatch(r':?-+:?', cell) for cell in cells):
            if len(cells) != width:
                raise ValueError(f'Строка «{cells[0]}» — ожидается {width} колонок.')
            if any(cells):
                found.append(cells)
    return found


def needs_input_table(note: str, kind: str) -> bool:
    try:
        return _table_lines(note, WORK_COLUMNS if kind == 'effort' else RISK_COLUMNS) is None
    except ValueError:
        # Уже заполненные строки исправляет автор. Дополнительная таблица скрыла бы ошибку.
        return False


def _ids(cell: str) -> tuple[str, ...]:
    if cell.strip() in ('', '—'):
        return ()
    return tuple(part.strip() for part in cell.split(',') if part.strip())


def estimate_from_notes(effort_note: str, risk_note: str):
    """Читает исходные таблицы для рабочего места и обзора ролей."""
    work_rows = _table_lines(effort_note, WORK_COLUMNS)
    risk_rows = _table_lines(risk_note, RISK_COLUMNS)
    if work_rows is None or risk_rows is None:
        missing = 'Оценка трудозатрат' if work_rows is None else 'Риски'
        raise ValueError(f'В документе «{missing}» отсутствует таблица исходных данных.')
    works = [WorkEstimate(*row[:7], _ids(row[7]) if len(row) > 7 else ()) for row in work_rows]
    risks = [RiskAllowance(row[0], _ids(row[1]), row[2], row[3]) for row in risk_rows]
    return estimate_effort(works, risks), risks


def render_estimate(effort_note: str, risk_note: str) -> str:
    """Строит результат из двух конкретных текстов, в том числе из снимков старой заявки."""
    title = f'Метод {METHOD}. Значения в чел·ч, округление до десятых после суммирования.\n\n'
    try:
        result, risks = estimate_from_notes(effort_note, risk_note)
    except ValueError as error:
        return title + f'**Исправьте исходные данные.** {error}\n\nИтог с резервом — нет данных.\n'

    lines = [title.rstrip(), '', f'**Итог с резервом — {display_hours(result.total)}'
             + (' чел·ч.**' if result.total is not None else '.**')]
    known = sum(row.mean is not None for row in result.rows)
    lines += ['', f'Оценено работ: {known} из {len(result.rows)}. '
              f'PERT по оценённым работам — {display_hours(result.base if known else None)}. '
              f'Отдельный резерв по заполненным решениям — {display_hours(result.reserve)}.']
    if result.missing:
        lines += ['', 'Для полного итога:', '', *('- ' + message for message in result.missing)]
    if result.rows:
        lines += ['', '| Работа | Роль | O | M | P | PERT | σ |', '| --- | --- | --- | --- | --- | --- | --- |']
        for row in result.rows:
            w = row.work
            values = [w.key, w.role, w.optimistic, w.likely, w.pessimistic,
                      display_hours(row.mean), display_hours(row.deviation)]
            lines.append('| ' + ' | '.join((v or '—').replace('|', r'\|') for v in values) + ' |')
    if risks:
        lines += ['', '| Риск | Работы | Решение РП | Добавлено, чел·ч |', '| --- | --- | --- | --- |']
        for risk in risks:
            values = (risk.key, ', '.join(risk.works), risk.treatment, display_hours(result.reserves[risk.key]))
            lines.append('| ' + ' | '.join(v.replace('|', r'\|') for v in values) + ' |')
    else:
        lines += ['', 'В таблице распределения резерва пока нет рисков.']
    return ('\n'.join(lines) + '\n\nPERT = (O + 4M + P) / 6. Разброс σ = (P − O) / 6.\n\n'
            'Срок и стоимость в этом расчёте — нет данных. Для них нужны календарь и тарифы.\n')


def calculation_for_snapshot(key: str, documents: dict) -> str:
    folder = f'Документы/{key}/'
    return render_estimate(documents[folder + 'Оценка трудозатрат.md']['text'],
                           documents[folder + 'Риски.md']['text'])
