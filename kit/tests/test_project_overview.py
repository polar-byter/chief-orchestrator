"""Рабочие поручения трёх ролей в одной базе и возвращение к исходному документу."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit import project_overview
from kit.journal import TaskJournal
from kit.project_docs import _parser, execute
from kit.project_overview import OverviewCache, overview_path, refresh_overview
from kit.project_revisions import accept_revision, request_revision
from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths, workspace_overview
from kit.role_tasks import ANALYST, ENGINEERS, MANAGER, RoleTask, missing_checks, open_tasks
from kit.task_control_state import write_state


class ProjectOverviewTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'проект'
        root.mkdir()
        self.vault = ProjectVault.connect(root)

    def feature(self, key='one', stage='stage'):
        self.vault.add('stage', stage, 'Этап')
        self.vault.add('feature', key, 'Поставка', stage)
        return section_paths(self.vault, key)

    def test_only_open_assignments_in_working_text(self):
        note = ('- [ ] #рп #РП Утвердить срок\n- [X] #рп Завершено\n'
                '````text\n```\n- [ ] #рп Пример\n````\n~~~\n- [ ] #аналитик Ещё пример\n~~~\n'
                ' - [ ] #аналитик #инженеры Проверить контракт\n- [ ] Передать Чифу\n'
                '- [ ] #рп/архив Другая метка\n- [ ] #рп')
        self.assertEqual(open_tasks(note), [RoleTask(MANAGER, 'Утвердить срок'),
                                           RoleTask(ANALYST, 'Проверить контракт'),
                                           RoleTask(ENGINEERS, 'Проверить контракт')])
        self.assertEqual(missing_checks('bdd', '```\n| B1 | x | y | Не проверено |\n```'), [])

    def test_empty_project_and_entry_points_preserve_home_and_source_snapshots(self):
        output = overview_path(self.vault)
        self.assertIn('ещё нет фич', output.read_text(encoding='utf-8'))
        self.assertIn('[[Три контура|', self.vault.home.read_text(encoding='utf-8'))
        paths = self.feature()
        original = {k: p.read_bytes() for k, p in paths.items()}
        self.assertIn('[[Три контура|', workspace_overview(self.vault, 'one').read_text(encoding='utf-8'))
        self.vault.home.write_text('План руководителя\n', encoding='utf-8')
        cmd = _parser().parse_args(['--root', str(self.vault.root), 'overview'])
        with patch('kit.obsidian_app.open_note') as opened, patch('kit.project_docs.ensure_inbox_watcher'):
            self.assertTrue(execute(cmd)['opened'])
            opened.assert_called_once_with(self.vault.path, output)
        refresh_overview(self.vault)
        self.assertTrue(self.vault.home.read_text(encoding='utf-8').startswith('План руководителя'))
        self.assertEqual(self.vault.home.read_text(encoding='utf-8').count('[[Три контура|'), 1)
        self.assertEqual(original, {k: p.read_bytes() for k, p in paths.items()})

    def test_same_titles_resolve_to_correct_section_feature_stage_and_revision(self):
        self.feature()
        docs = self.feature('two', 'next')
        for kind, line in (('risks', '#рп Согласовать резерв'), ('bdd', '#аналитик Уточнить отказ'),
                           ('nfr', '#инженеры Проверить порог')):
            with docs[kind].open('a', encoding='utf-8') as out:
                out.write('\n- [ ] ' + line + '\n')
        note = refresh_overview(self.vault).read_text(encoding='utf-8')
        for role, action, destination in ((MANAGER, 'Согласовать резерв', 'Риски'), (ANALYST, 'Уточнить отказ', 'BDD'),
                                          (ENGINEERS, 'Проверить порог', 'NFR')):
            role_body = note.split('\n## ' + role + '\n', 1)[1].split('\n## ', 1)[0]
            line, = [line for line in role_body.splitlines() if action in line]
            self.assertIn(f'[[Документы/two/{destination}|', line)
            self.assertIn('two · next · [[История/two/Редакция 0001|ред. 1]]', line)
            self.assertTrue((self.vault.path / 'Документы/two' / (destination + '.md')).is_file())

    def test_resave_and_completion_drop_task_and_missing_source_does_not_hide_neighbour(self):
        docs = self.feature()
        other = self.feature('two')
        cache = OverviewCache()
        path = docs['risks']
        path.write_text('- [ ] #рп Решить сегодня', encoding='utf-8')
        other['nfr'].write_text('- [ ] #инженеры Проверить нагрузку', encoding='utf-8')
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertIn('Решить сегодня', note)
        path.write_text('- [x] #рп Решить сегодня', encoding='utf-8')
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertNotIn('Решить сегодня', note)
        docs['bdd'].unlink()
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertIn('Недоступные документы', note)
        self.assertIn('Проверить нагрузку', note)
        self.assertFalse(docs['bdd'].exists())
        refresh_overview(self.vault, cache)
        with patch.object(project_overview, '_render', side_effect=AssertionError('лишнее чтение')):
            refresh_overview(self.vault, cache)

    def test_live_budget_and_criteria_match_feature_sources(self):
        paths = self.feature()
        for kind, old, new in (('effort', '| | | | | | | | |', '| W1 | Инженер | чел·ч | 8 | 12 | 24 | AC | |'),
                               ('risks', '|  |  |  |  |', '| R1 | W1 | отдельно | 2 |')):
            text = paths[kind].read_text(encoding='utf-8')
            self.assertIn(old, text)
            paths[kind].write_text(text.replace(old, new), encoding='utf-8')
        cache = OverviewCache()
        result = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertIn('С резервом 15,3 чел·ч · риски R1', result)
        self.assertIn('BDD-01 · отсутствует результат проверки', result)
        paths['nfr'].write_text('Уточнён порог.', encoding='utf-8')
        result = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        changed, = [line for line in result.splitlines() if 'Документ изменён' in line]
        self.assertIn('[[Документы/one/NFR|NFR]]', changed)
        self.assertNotIn('отсутствует замер', result)

    def test_only_feature_request_drives_engineering_state(self):
        self.feature()
        with self.vault.workspace('one').open('a', encoding='utf-8') as note:
            note.write('Запросить проверку задержки.')
        request = request_revision(self.vault, 'one')
        cache = OverviewCache()
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertIn(request['id'], note)
        self.assertIn('Ожидает начала', note)
        journal = TaskJournal(self.vault.root)
        directory = journal.task_dir(request['id'])
        directory.mkdir()
        journal.append(request['id'], 'start')
        journal.append(request['id'], 'test', status='skipped')
        write_state(self.vault.root, request['id'], status='pausing')
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertIn('есть пропуски', note)
        self.assertIn('сверить остановку', note)
        self.assertIn((directory / 'events.jsonl').as_uri(), note)
        accept_revision(self.vault, 'one', request['id'], 'Результат', 'Наблюдение')
        note = refresh_overview(self.vault, cache).read_text(encoding='utf-8')
        self.assertNotIn(request['id'], note)
        self.assertIn('Принятое решение', note)
        self.assertIn('Редакция 0002|ред. 2', note)

    def test_foreign_report_is_preserved_on_refresh(self):
        path = overview_path(self.vault)
        path.write_text('Мои задачи', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'другой текст'):
            refresh_overview(self.vault)
        self.assertEqual(path.read_text(encoding='utf-8'), 'Мои задачи')
