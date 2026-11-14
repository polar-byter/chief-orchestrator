"""Обратный переход из хранилища Obsidian в работающий дашборд проекта."""
from urllib.parse import urlencode

from ..project_workspace import SECTIONS, workspace_overview
from ..vault_reports import append_note, publish_report


class WorkspaceNavigation:
    def __init__(self, base_url):
        self.base_url = base_url
        self.published = None

    def refresh(self, vault, records):
        signature = (vault.path, vault.project_id, tuple((r['key'], r['parent'], r['title']) for r in records))
        if self.published == signature:
            return
        with vault.locked():
            sections = ['# Монитор Чифа', '', 'Выберите рабочее место или раздел документа.']
            for record in records:
                key = record['key']
                stage = record['parent'] or key
                args = {'stage': stage, 'project': vault.project_id}
                if record['parent']:
                    args['feature'] = key
                def link(label, **context):
                    return f'[{label}]({self.base_url}/stage?{urlencode(dict(args, **context))})'

                sections.extend(('', f'## {key}', '', record['title'], '',
                                 ' · '.join(link(label, team=role) for label, role in (
                                     ('Управление', 'manager'), ('Анализ', 'analyst'),
                                     ('Инженеры', 'engineer'), ('Совместная работа', 'joint'))), '',
                                 ' · '.join(link(s.title, section=s.key, team='manager' if s.key in ('risks', 'effort') else 'analyst')
                                            for s in SECTIONS)))
            publish_report(vault.path / 'Монитор Чифа.md', vault.project_id + '/dashboard-links',
                           '\n'.join(sections) + '\n')
            for record in records:
                key = record['key']
                _append_link(workspace_overview(vault, key), f'[[Монитор Чифа#{key}|Открыть в дашборде Чифа]]')
            _append_link(vault.home, '[[Монитор Чифа|Открыть монитор этапа в дашборде]]')
        self.published = signature


def _append_link(path, link):
    if path.is_file() and link.encode('utf-8') not in path.read_bytes():
        append_note(path, '\n\n' + link + '\n')
