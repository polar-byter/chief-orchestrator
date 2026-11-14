"""Обновление расчёта в базе проекта без перезаписи исходных таблиц."""
from __future__ import annotations

import sys
from pathlib import Path

from .estimate_documents import RESULT_NAME, needs_input_table, render_estimate, setup_text
from .vault_reports import append_note, publish_report
from .task_control_state import plain_path


def result_path(folder: Path) -> Path:
    return plain_path(folder / (RESULT_NAME + '.md'))


def _embed(key: str) -> str:
    return f'![[Документы/{key}/{RESULT_NAME}#Сводка]]'


def input_addition(note: str, key: str, kind: str) -> str:
    """Точный добавленный хвост позволяет сверить заявки, созданные до обновления."""
    embed = _embed(key)
    if embed in note:
        return ''
    setup = setup_text(kind) if needs_input_table(note, kind) else ''
    return setup + '\n\n## Расчёт по работам и рискам\n\n' + embed + '\n'


def prepare_estimate(vault, key: str, paths: dict[str, Path], overview: Path) -> None:
    """Вызывается под блокировкой базы, включая повтор после прерванного обновления."""
    for kind in ('effort', 'risks'):
        path = paths[kind]
        append_note(path, input_addition(path.read_text(encoding='utf-8-sig'), key, kind))
    embed = _embed(key)
    if embed not in overview.read_text(encoding='utf-8-sig'):
        append_note(overview, f'\n\n## Трудозатраты и резерв\n\n'
                f'РП — [[Документы/{key}/Риски|решения по рискам и резерву]]. '
                f'Аналитик — [[Документы/{key}/Требования|границы оцениваемых работ]]. '
                f'Инженерная команда — [[Документы/{key}/Оценка трудозатрат|исходные оценки O/M/P]].\n\n'
                + embed + '\n')
    write_estimate(vault.project_id, key, overview.parent)


def write_estimate(project_id: str, key: str, folder: Path) -> str:
    """Публикует только производный файл. Вызывающая сторона сериализует записи в базу."""
    output = result_path(folder)
    inputs = []
    for name in ('Оценка трудозатрат', 'Риски'):
        try:
            inputs.append(plain_path(folder / (name + '.md')).read_text(encoding='utf-8-sig'))
        except (FileNotFoundError, UnicodeError):
            report = f'**Исправьте исходные данные.** Восстановите документ «{name}» в UTF-8.\n'
            break
        except (OSError, ValueError) as error:
            report = f'**Расчёт недоступен.** Не удалось прочитать «{name}». {error}\n'
            break
    else:
        report = render_estimate(*inputs)
    links = (f'[[Документы/{key}/Оценка трудозатрат|Оценка инженера]] · '
             f'[[Документы/{key}/Риски|Решения РП]] · [[Документы/{key}/Рабочее место|Рабочее место]]')
    publish_report(output, f'{project_id}/{key}/pert-v1', '## Сводка\n\n' + links + '\n\n' + report)
    return report


def refresh_estimates(vault, keys, cache: dict) -> None:
    """В простое проверяет только признаки изменения двух входов и файла отчёта."""
    for key in keys:
        try:
            _refresh_changed(vault, key, cache)
        except (OSError, ValueError) as error:
            # Отказ доступа к одному каталогу не должен останавливать очередь заявок.
            if cache.get(key) != str(error):
                print(f'Расчёт {key}: {error}', file=sys.stderr, flush=True)
                cache[key] = str(error)


def _refresh_changed(vault, key: str, cache: dict) -> None:
    folder = plain_path(vault.path / 'Документы' / key)
    signatures = []
    for name in ('Оценка трудозатрат.md', 'Риски.md', RESULT_NAME + '.md'):
        try:
            stat = (folder / name).lstat()
            signatures.append((stat.st_mtime_ns, stat.st_size, stat.st_ino))
        except FileNotFoundError:
            signatures.append(None)
    if signatures != cache.get(key) and folder.is_dir():
        with vault.locked():
            write_estimate(vault.project_id, key, folder)
        cache[key] = signatures
