"""Ответственность за поручения в заметках проектной базы."""
from __future__ import annotations

from dataclasses import dataclass
import re


OVERVIEW_TITLE = 'Три контура'
MANAGER = 'Руководитель проекта'
ANALYST = 'Аналитик'
ENGINEERS = 'Инженерная команда'
ROLE_LABELS = {MANAGER: 'РП', ANALYST: 'Аналитик', ENGINEERS: 'Инженеры'}
_TAGS = {'рп': MANAGER, 'аналитик': ANALYST, 'инженеры': ENGINEERS}
_CHECKBOX = re.compile(r'^\s*[-*+]\s+\[([ xX])\]\s+(.+)$')
_ROLE_TAG = re.compile(r'(?<!\S)#(рп|аналитик|инженеры)(?=\s|$)', re.IGNORECASE)


@dataclass(frozen=True)
class RoleTask:
    role: str
    text: str


@dataclass(frozen=True)
class Assignment:
    roles: tuple[str, ...]
    text: str
    done: bool


def document_lines(note: str):
    """Возвращает рабочий текст заметки, исключая примеры в блоках кода."""
    fence = ''
    for line in note.splitlines():
        stripped = line.lstrip()
        delimiter = re.match(r'^(`{3,}|~{3,})', stripped)
        if delimiter:
            if not fence:
                fence = delimiter[1]
            elif stripped.rstrip().startswith(fence) and not stripped.rstrip().strip(fence[0]):
                fence = ''
            continue
        if not fence:
            yield line


def assignments(note: str):
    """Хранит общий флажок у нескольких ролей как одно поручение."""
    for line in document_lines(note):
        match = _CHECKBOX.fullmatch(line)
        if not match:
            continue
        description = _ROLE_TAG.sub('', match[2]).strip()
        roles = tuple(dict.fromkeys(_TAGS[tag.casefold()] for tag in _ROLE_TAG.findall(match[2])))
        if description and roles:
            yield Assignment(roles, description, match[1] != ' ')


def open_tasks(note: str) -> list[RoleTask]:
    return [RoleTask(role, task.text) for task in assignments(note) if not task.done for role in task.roles]


def missing_checks(section: str, note: str) -> list[str]:
    """Отражает только явные пустые результаты в таблицах BDD или паспорте NFR."""
    missing = []
    for line in document_lines(note):
        if not line.strip().startswith('|'):
            continue
        cells = [value.strip() for value in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
        if (section == 'bdd' and len(cells) == 4 and cells[0]
                and cells[-1].casefold() in ('не проверено', 'нет данных')):
            missing.append(f'{cells[0]} · отсутствует результат проверки')
        if (section == 'nfr' and len(cells) == 2 and cells[0] == 'Фактическое значение'
                and cells[1].casefold() in ('не измерено', 'нет данных')):
            missing.append('В паспорте качества отсутствует замер')
    return missing
