"""Документация проекта и передача доработок из Obsidian в Чиф."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .project_inbox import ensure_inbox_watcher, retry_delivery, stop_inbox_watcher
from .project_revisions import accept_revision, compare_revision, pending_requests, request_revision
from .project_vault import ProjectVault
from .project_overview import refresh_overview
from .project_workspace import SECTIONS, ensure_workspace, section_paths
from .workspace_estimates import result_path
from . import native_chief


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='каталог прикладного проекта')
    actions = parser.add_subparsers(dest='action', required=True)
    overview = actions.add_parser('overview', help='обновить и открыть обзор «Три контура»')
    overview.add_argument('--no-open', action='store_true', help='обновить обзор без открытия окна')
    init = actions.add_parser('init', help='создать или подключить базу и открыть проект')
    init.add_argument('--vault', type=Path, help='новая папка или перенесённая база того же проекта')
    init.add_argument('--name', help='название проекта при создании')
    init.add_argument('--no-open', action='store_true', help='подготовить базу без запуска приложения')
    opening = actions.add_parser('open', help='открыть проект или выбранную запись')
    opening.add_argument('--key', help='ID этапа или фичи')
    destination = opening.add_mutually_exclusive_group()
    destination.add_argument('--work', action='store_true', help='открыть рабочую заметку для доработки')
    destination.add_argument('--section', choices=('workspace', 'estimate', *(item.key for item in SECTIONS)),
                             help='открыть рабочее место или один из документов записи')
    add = actions.add_parser('add', help='добавить карточку принятого в план этапа или фичи')
    add.add_argument('kind', choices=('stage', 'feature'))
    add.add_argument('--key', required=True)
    add.add_argument('--title', required=True)
    add.add_argument('--description', default='')
    add.add_argument('--parent', help='ID этапа для фичи')
    actions.add_parser('list', help='показать состав проекта')
    configure = actions.add_parser('configure', help='посмотреть или выбрать агента для доставки заявок из этой базы')
    configure.add_argument('--agent', choices=('default', *native_chief.TRANSPORTS),
                           help='default использует настроенный шлюз OpenCode или Pi')
    probe = actions.add_parser('probe', help='проверить команды установленного CLI без обращения к модели')
    probe.add_argument('--agent', choices=native_chief.TRANSPORTS, required=True)
    actions.add_parser('stop', help='остановить наблюдение за флажками этой базы')
    submit = actions.add_parser('submit', help='сохранить запрос из рабочей заметки в очередь Чифа')
    submit.add_argument('--key', required=True)
    actions.add_parser('pending', help='прочитать заявки, ожидающие решения')
    compare = actions.add_parser('compare', help='сравнить сохранённую заявку с текущими заметками')
    compare.add_argument('--request', required=True)
    send = actions.add_parser('send', help='передать сохранённую заявку настроенной среде')
    send.add_argument('--request', required=True, help='ID заявки из очереди')
    send.add_argument('--resume', help='UUID подтверждённой нативной сессии этой же записи')
    accept = actions.add_parser('accept', help='записать проверенный результат и новую редакцию')
    accept.add_argument('--key', required=True)
    accept.add_argument('--request', required=True)
    accept.add_argument('--result', required=True)
    accept.add_argument('--evidence', required=True)
    basis = accept.add_mutually_exclusive_group(required=True)
    basis.add_argument('--task', help='ID завершённой задачи с успешными полными тестами')
    basis.add_argument('--manual', action='store_true', help='записать проверку оператором')
    return parser


def execute(args):
    if args.action == 'probe':
        from .runtimes import native_worker
        return dict(native_chief.TRANSPORTS[args.agent].probe(), managed_team=native_worker.probe(args.agent))
    if args.action == 'init':
        vault = ProjectVault.connect(args.root, args.vault, args.name)
    else:
        vault = ProjectVault.load(args.root)
    if args.action in ('open', 'init', 'overview'):
        from .obsidian_app import open_note
        note = vault.home
        if args.action == 'init':
            for record in vault.records():
                ensure_workspace(vault, record['key'])
        if args.action == 'overview':
            note = refresh_overview(vault)
        elif args.action == 'init' or (args.action == 'open' and not args.key):
            refresh_overview(vault)
        if args.action == 'open':
            if (args.work or args.section) and not args.key:
                raise ValueError('Для раздела или доработки нужен ID этапа или фичи в --key.')
            if args.key:
                overview = ensure_workspace(vault, args.key)
                note = vault.workspace(args.key) if args.work else vault.card(args.key)
                if args.section:
                    notes = dict(section_paths(vault, args.key), workspace=overview, estimate=result_path(overview.parent))
                    note = notes[args.section]
        opened = not getattr(args, 'no_open', False)
        if opened:
            open_note(vault.path, note)
            ensure_inbox_watcher(vault)
        return dict(project=vault.project_id, vault=str(vault.path), note=str(note), opened=opened)
    if args.action == 'add':
        return dict(note=str(vault.add(args.kind, args.key, args.title, args.parent, args.description)))
    if args.action == 'list':
        return vault.records()
    if args.action == 'configure':
        if args.agent:
            ensure_inbox_watcher(vault, only_if_running=True)
        return native_chief.configure(vault, args.agent)
    if args.action == 'stop':
        stop_inbox_watcher(vault)
        return dict(project=vault.project_id, stop_requested=True)
    if args.action == 'submit':
        return request_revision(vault, args.key)
    if args.action == 'pending':
        return pending_requests(vault)
    if args.action == 'compare':
        return compare_revision(vault, args.request)
    if args.action == 'send':
        request = next((r for r in pending_requests(vault) if r['id'] == args.request), None)
        if request is None:
            raise ValueError('В очереди проекта нет открытой заявки с этим ID.')
        return retry_delivery(vault, request, resume=args.resume)
    return accept_revision(vault, args.key, args.request, args.result, args.evidence, task=args.task)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = execute(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f'Документация проекта: {error}', file=sys.stderr)
        return 1
    print(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
