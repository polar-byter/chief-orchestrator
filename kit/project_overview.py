"""Обзор «Три контура» из проектных документов, редакций и журналов заявок."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re

from .effort_estimate import display_hours
from .estimate_documents import estimate_from_notes
from .journal import TaskJournal
from .project_revisions import overview_revisions
from .project_vault import DATABASE_FILE, HOME_NOTE
from .project_workspace import SECTIONS
from .role_tasks import ANALYST, ENGINEERS, MANAGER, OVERVIEW_TITLE, ROLE_LABELS, missing_checks, open_tasks
from .task_control_state import plain_path, read_state, state_path
from .vault_reports import append_note, publish_report


@dataclass
class OverviewCache:
    signature: tuple | None = None
    task_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Entry:
    feature: str
    context: str
    action: str
    destination: str

    def line(self) -> str:
        return f'- **{self.feature}** · {self.action} · {self.destination} ({self.context})'


def overview_path(vault) -> Path:
    return plain_path(vault.path / (OVERVIEW_TITLE + '.md'))


def _plain(text: str) -> str:
    # Текст поручения показываем буквально. Ссылку создаём из пути зарегистрированной фичи.
    return re.sub(r'([\\`*_\[\]<>|~])', r'\\\1', ' '.join(text.split()))


def _link(path: Path, label: str) -> str:
    return f'[[{path.with_suffix("").as_posix()}|{label}]]'


def _file_state(path: Path) -> tuple:
    try:
        info = path.lstat()
        return (info.st_mtime_ns, info.st_size, info.st_ino)
    except FileNotFoundError:
        return ()
    except OSError as error:
        return ('недоступен', error.errno)


def _signature(vault, features: list[dict], task_ids: set[str]) -> tuple:
    database = vault.path / DATABASE_FILE
    files = [database, database.with_name(database.name + '-wal'), overview_path(vault), vault.home]
    for item in features:
        folder = vault.path / 'Документы' / item['key']
        files.extend(folder / (section.title + '.md') for section in SECTIONS)
    journal = TaskJournal(vault.root)
    for task_id in sorted(task_ids):
        try:
            files.extend((journal.task_dir(task_id) / 'events.jsonl', state_path(vault.root, task_id)))
        except ValueError:
            # В списке поручений останется причина отказа. Отдельная задача не блокирует остальные.
            files.append(vault.root / '.metrics' / task_id)
    return tuple(_file_state(p) for p in files)


def _journal_summary(root: Path, task_id: str) -> str:
    journal = TaskJournal(root)
    path = plain_path(journal.task_dir(task_id) / 'events.jsonl')
    if not path.is_file():
        return 'Ожидает начала работы'
    events = journal.read(task_id)
    attempts = sum(event['kind'] == 'delegate' for event in events)
    tests = [event for event in events if event['kind'] == 'test']
    summary = 'Задача завершена' if events and events[-1]['kind'] == 'finish' else 'Задача открыта'
    summary += f', поручений исполнителям — {attempts}'
    if tests:
        last = tests[-1]
        outcomes = {'passed': 'пройдена', 'failed': 'ошибка', 'skipped': 'есть пропуски'}
        summary += ', последняя проверка — ' + outcomes.get(last.get('status'), 'см. журнал')
    else:
        summary += ', результатов тестов пока нет'
    control = read_state(root, task_id).get('status')
    if control in ('pausing', 'cancelling', 'error'):
        summary += ', требуется сверить остановку в дашборде'
    return summary + f'. [Журнал]({path.as_uri()})'


@dataclass
class RoleOverview:
    assigned: dict = field(default_factory=lambda: {role: [] for role in ROLE_LABELS})
    signals: dict = field(default_factory=lambda: {role: [] for role in ROLE_LABELS})
    issues: list[str] = field(default_factory=list)


def _feature_entries(vault, feature, revision, request, overview: RoleOverview):
    key = feature['key']
    folder = Path('Документы', key)
    title = _plain(feature['title'])
    number = revision.get('revision')
    history = (_link(Path('История', key, f'Редакция {number:04}.md'), f'ред. {number}')
               if number else 'редакция ещё не записана')
    context = f'{key} · {feature["parent"]} · {history}'
    documents = {}
    for section in SECTIONS:
        relative = folder / (section.title + '.md')
        try:
            content = plain_path(vault.path / relative).read_bytes()
            note = content.decode('utf-8-sig')
        except (OSError, ValueError) as error:
            overview.issues.append(f'{title} ({key}) · {section.title} · {_plain(str(error))}')
            continue
        documents[section.key] = note
        link = _link(relative, section.title)
        for task in open_tasks(note):
            overview.assigned[task.role].append(Entry(title, context, _plain(task.text), link))
        for message in missing_checks(section.key, note):
            overview.signals[ANALYST].append(Entry(title, context, _plain(message), link))
        snapshot = revision.get('documents', {}).get(relative.as_posix())
        if section.key in ('requirements', 'bdd', 'nfr') and snapshot:
            if hashlib.sha256(content).hexdigest() != snapshot['sha256']:
                overview.signals[ANALYST].append(Entry(title, context, 'Документ изменён после указанной редакции', link))

    if 'effort' in documents and 'risks' in documents:
        try:
            result, risks = estimate_from_notes(documents['effort'], documents['risks'])
        except ValueError as error:
            overview.signals[MANAGER].append(Entry(title, context, _plain(str(error)), _link(folder / 'Риски.md', 'Риски')
                                          + ' · ' + _link(folder / 'Оценка трудозатрат.md', 'Оценка')))
        else:
            effort = _link(folder / 'Расчёт трудозатрат.md', 'Расчёт')
            risk_link = _link(folder / 'Риски.md', 'Риски')
            total = 'Оценка неполная' if result.total is None else f'С резервом {display_hours(result.total)} чел·ч'
            detail = ', '.join(_plain(risk.key) for risk in risks)
            overview.signals[MANAGER].append(Entry(title, context, total + (f' · риски {detail}' if detail else ''),
                                          effort + ' · ' + risk_link))
    if request:
        task_id = request['task_id']
        try:
            summary = _journal_summary(vault.root, task_id)
        except (OSError, ValueError) as error:
            summary = 'Журнал недоступен. ' + _plain(str(error))
        overview.signals[ENGINEERS].append(Entry(title, context, f'Заявка `{task_id}` · ' + summary,
                                        _link(vault.workspace(key).relative_to(vault.path), 'Доработка')))
    if revision.get('status') == 'accepted':
        overview.signals[ENGINEERS].append(Entry(title, context, 'Принятое решение и основание проверки', history))


def _render(vault, features, latest, requests):
    overview = RoleOverview()
    for feature in sorted(features, key=lambda item: (item['parent'], item['title'].casefold(), item['key'])):
        key = feature['key']
        _feature_entries(vault, feature, latest.get(key, {}), requests.get(key), overview)
    lines = [_link(Path(HOME_NOTE), 'Документация проекта'), '',
             ' · '.join(f'[[#{role}|{label} ({len(overview.assigned[role])})]]' for role, label in ROLE_LABELS.items()), '']
    if not features:
        lines += ['В проекте ещё нет фич. Добавьте фичу в этап, чтобы вести поручения.', '']
    for role in ROLE_LABELS:
        lines += ['## ' + role, '', '### Открытые поручения', '']
        lines += [entry.line() for entry in overview.assigned[role]] or ['Открытых поручений нет.']
        lines += ['', '### ' + {MANAGER: 'Оценки и риски', ANALYST: 'Условия и проверки',
                               ENGINEERS: 'Заявки и решения'}[role], '']
        lines += [entry.line() for entry in overview.signals[role]] or ['Записей для показа нет.']
        lines.append('')
    if overview.issues:
        lines += ['## Недоступные документы', '', *('- ' + issue for issue in overview.issues), '']
    lines += ['## Как назначить поручение', '',
              'В одном из пяти документов фичи добавьте строку '
              '`- [ ] #рп Согласовать резерв`. Метки ролей — `#рп`, `#аналитик`, `#инженеры`. '
              'Нажмите ссылку нужного раздела и отметьте выполнение в исходной заметке.']
    return '\n'.join(lines) + '\n'


def refresh_overview(vault, cache: OverviewCache | None = None) -> Path:
    """Перестраивает обзор под общей блокировкой. В простое достаточно проверки метаданных."""
    output = overview_path(vault)
    features = [r for r in vault.records() if r['kind'] == 'feature']
    if cache is not None and cache.signature == _signature(vault, features, cache.task_ids):
        return output
    with vault.locked() as db:
        features = [dict(row) for row in db.execute("SELECT * FROM records WHERE kind='feature'")]
        # Снимаем признаки до чтения файлов. Внешнее сохранение во время построения
        # обзора обнаружится следующим проходом, даже если редактор пишет без блокировки.
        latest, requests = overview_revisions(db)
        task_ids = {r['task_id'] for r in requests.values() if re.fullmatch(r'doc-[0-9a-f]{32}', r['task_id'])}
        signature = _signature(vault, features, task_ids) if cache is not None else None
        body = _render(vault, features, latest, requests)
        publish_report(output, vault.project_id + '/three-contours-v1', body)
        home_link = _link(Path(OVERVIEW_TITLE + '.md'), 'Три контура')
        if home_link not in vault.home.read_text(encoding='utf-8-sig'):
            append_note(vault.home, '\n\n' + home_link + ' — поручения РП, аналитика и инженерной команды.\n')
        if cache is not None:
            cache.task_ids, cache.signature = task_ids, signature
    return output
