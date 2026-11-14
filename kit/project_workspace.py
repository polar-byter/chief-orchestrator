"""Документы одной возможности и навигация по её рабочему месту в Obsidian."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path

from .project_vault import publish_new
from .role_tasks import OVERVIEW_TITLE
from .task_control_state import plain_path
from .vault_reports import append_note
from .workspace_estimates import input_addition, prepare_estimate


@dataclass(frozen=True)
class WorkspaceSection:
    key: str
    title: str
    purpose: str


SECTIONS = (
    WorkspaceSection('requirements', 'Требования', 'Цель, границы, правила и решения'),
    WorkspaceSection('bdd', 'BDD', 'Примеры поведения и критерии приёмки'),
    WorkspaceSection('nfr', 'NFR', 'Качество, условия измерения и результаты'),
    WorkspaceSection('effort', 'Оценка трудозатрат', 'Объём работ, исходные оценки и допущения'),
    WorkspaceSection('risks', 'Риски', 'Причины, последствия, меры и владельцы'),
)


def section_paths(vault, key: str) -> dict[str, Path]:
    """ID проверяется хранилищем до построения любого пути."""
    record = vault.record(key)
    folder = vault.path / 'Документы' / record['key']
    return {section.key: plain_path(folder / (section.title + '.md')) for section in SECTIONS}


def workspace_overview(vault, key: str) -> Path:
    vault.record(key)
    return plain_path(vault.path / 'Документы' / key / 'Рабочее место.md')


def _link(vault, path: Path, label: str) -> str:
    return f'[[{path.relative_to(vault.path).with_suffix("").as_posix()}|{label}]]'


def _navigation(key: str) -> str:
    lines = ['| Документ | Содержание |', '| --- | --- |']
    for section in SECTIONS:
        # В таблице Obsidian разделитель имени ссылки нужно экранировать.
        link = f'[[Документы/{key}/{section.title}\\|{section.title}]]'
        lines.append(f'| {link} | {section.purpose} |')
    return '\n'.join(lines) + '\n'


def card_navigation(key: str) -> str:
    """Один и тот же блок нужен при обновлении карточки и сверке старой заявки."""
    return ('\n\n## Рабочее место\n\n'
            f'[[Документы/{key}/Рабочее место|Открыть рабочее место]]\n\n' + _navigation(key))


def _backlinks(vault, record: dict, *, include_overview: bool) -> str:
    key = record['key']
    links = [_link(vault, vault.card(key), 'Карточка'),
             f'[[История/{key}/Редакции|История редакций]]']
    if include_overview:
        links.insert(0, _link(vault, workspace_overview(vault, key), 'Рабочее место'))
    if record['parent']:
        links.append(_link(vault, vault.card(record['parent']), 'Этап'))
    return ' · '.join(links)


def ensure_workspace(vault, key: str) -> Path:
    """Создаёт отсутствующее рабочее место, сохраняя любые правки готовых заметок.

    Маркер завершения отличает прерванное создание от удаления документа
    пользователем. Во втором случае нужен исходный файл, а не пустой шаблон.
    Карточку дополняем в конец: черновик заявки и прежний текст не переписываются.
    """
    with vault.locked():
        record = vault.record(key)
        overview = workspace_overview(vault, key)
        paths = section_paths(vault, key)
        marker = plain_path(vault.path / '.chief' / 'workspaces' / (key + '.json'))
        if marker.exists():
            for path in (overview, *paths.values()):
                if not path.is_file():
                    raise ValueError(f'Документ рабочего места отсутствует: {path}. Восстановите его из своей копии.')
        else:
            backlinks = _backlinks(vault, record, include_overview=True)
            for section in SECTIONS:
                template = (Path(__file__).parent / 'templates' / 'project-workspace' /
                            (section.key + '.md')).read_text(encoding='utf-8')
                text = (f'# {section.title} · {record["title"]}\n\n{backlinks}\n\n'
                        f'Запись: `{key}`. Статус документа: черновик.\n\n' + template)
                publish_new(paths[section.key], text.encode('utf-8'))
            text = (f'# Рабочее место · {record["title"]}\n\n'
                    + _backlinks(vault, record, include_overview=False) + '\n\n'
                    + _navigation(key) + '\n'
                    + _link(vault, vault.workspace(key), 'Запросить доработку') + '\n\n'
                    'Редактируйте документы напрямую. Для передачи Чифу откройте доработку, '
                    'опишите ожидаемый результат и отметьте «Передать Чифу». Заявка сохранит '
                    'снимки всех пяти документов. Изменение заметки само по себе не означает приёмку.\n\n'
                    'РП отвечает за объём, оценку и риски. Аналитик — за требования, BDD и '
                    'критерии NFR. Инженерный контур — за архитектуру, исполнение и свидетельства проверки.\n')
            publish_new(overview, text.encode('utf-8'))
            publish_new(marker, b'{"format": 1}\n')
        card = plain_path(vault.card(key))
        # Добавление в конец не заменяет сохранённый редактором текст. Блокировка
        # базы исключает дублирование между одновременными открытиями Чифа.
        if f'[[Документы/{key}/Рабочее место|'.encode() not in card.read_bytes():
            block = card_navigation(key).encode('utf-8')
            # Нет O_CREAT: исчезнувшая карточка не должна стать заметкой из одних ссылок.
            flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_BINARY', 0)
            with os.fdopen(os.open(card, flags), 'wb') as output:
                output.write(block)
        prepare_estimate(vault, key, paths, overview)
        project_link = f'[[{OVERVIEW_TITLE}|Поручения трёх контуров]]'
        if project_link not in overview.read_text(encoding='utf-8-sig'):
            append_note(overview, '\n\n' + project_link + '\n')
        return overview


def document_snapshot(vault, key: str, *, legacy_documents: dict | None = None) -> dict[str, dict[str, str]]:
    """Читает пять документов буквально; отсутствие файла не превращается в пустой текст."""
    result = {}
    for kind, path in section_paths(vault, key).items():
        content = path.read_bytes()
        name = path.relative_to(vault.path).as_posix()
        if legacy_documents and kind in ('effort', 'risks'):
            # Старая заявка проверяется по первоначальным байтам документа.
            # Убирается только точный хвост обновления; правки чисел сохраняются в сравнении.
            addition = input_addition(legacy_documents[name]['text'], key, kind)
            content = content.removesuffix(addition.encode('utf-8'))
        result[name] = {
            'text': content.decode('utf-8-sig'), 'sha256': hashlib.sha256(content).hexdigest()}
    return result
