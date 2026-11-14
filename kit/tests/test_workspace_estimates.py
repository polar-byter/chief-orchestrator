"""Пересчёт заметок, старые заявки и сохранность пользовательских данных."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kit.project_revisions import accept_revision, compare_revision, request_revision
from kit.project_vault import ProjectVault
from kit.project_workspace import ensure_workspace, section_paths
from kit.workspace_estimates import refresh_estimates, result_path


class WorkspaceEstimateTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'код'
        root.mkdir()
        self.vault = ProjectVault.connect(root)
        self.vault.add('stage', 'stage', 'Этап')
        self.key = 'stage'
        self.paths = section_paths(self.vault, self.key)
        self.output = result_path(self.paths['effort'].parent)

    def replace(self, kind, old, new):
        path = self.paths[kind]
        before = path.read_text(encoding='utf-8')
        self.assertIn(old, before)
        path.write_text(before.replace(old, new, 1), encoding='utf-8')

    def test_editing_either_input_replaces_obsolete_total_and_repairs_after_conflict(self):
        cache = {}
        self.replace('effort', '| | | | | | | | |', '| W1 | Инженер | чел·ч | 8 | 12 | 24 | AC-14 | R1 |')
        self.replace('risks', '|  |  |  |  |', '| R1 | W1 | в O/M/P | |')
        refresh_estimates(self.vault, [self.key], cache)
        self.assertIn('13,3 чел·ч', self.output.read_text(encoding='utf-8'))
        self.replace('risks', 'R1 | W1 | в O/M/P | |', 'R1 | W1 | отдельно | 2 |')
        refresh_estimates(self.vault, [self.key], cache)
        note = self.output.read_text(encoding='utf-8')
        self.assertIn('R1 уже учтён', note)
        self.assertNotIn('13,3', note)
        self.replace('effort', 'AC-14 | R1 |', 'AC-14 | |')
        refresh_estimates(self.vault, [self.key], cache)
        self.assertIn('15,3 чел·ч', self.output.read_text(encoding='utf-8'))
        self.paths['effort'].unlink()
        refresh_estimates(self.vault, [self.key], cache)
        self.assertIn('Восстановите документ', self.output.read_text(encoding='utf-8'))

    def test_generated_report_preserves_foreign_note_and_recreates_missing_projection(self):
        self.output.write_text('Заметка руководителя', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'другой текст'):
            ensure_workspace(self.vault, self.key)
        self.assertEqual(self.output.read_text(encoding='utf-8'), 'Заметка руководителя')
        moved = self.output.with_name('Мои заметки.md')
        self.output.rename(moved)
        ensure_workspace(self.vault, self.key)
        self.assertTrue(self.output.is_file())
        self.assertEqual(moved.read_text(encoding='utf-8'), 'Заметка руководителя')

    def test_history_uses_frozen_result_and_source_edits_block_acceptance(self):
        self.replace('effort', '| | | | | | | | |', '| W1 | Инженер | чел·ч | 8 | 12 | 24 | Проверка | |')
        with self.vault.workspace(self.key).open('a', encoding='utf-8') as f:
            f.write('Оценить работы.')
        req = request_revision(self.vault, self.key)
        self.assertIn('13,3 чел·ч', req['estimate'])
        self.replace('effort', '8 | 12 | 24', '2 | 3 | 4')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            accept_revision(self.vault, self.key, req['id'], 'Изменено', 'Сверка')
        latest = request_revision(self.vault, self.key)
        accepted = accept_revision(self.vault, self.key, latest['id'], 'Изменено', 'Сверка')
        archive = (self.vault.path / accepted['note']).read_text(encoding='utf-8')
        self.assertIn('3,0 чел·ч', archive)
        self.assertNotIn('13,3 чел·ч', archive)

    def test_upgrade_preserves_previous_request_and_detects_later_manual_edit(self):
        with self.vault.workspace(self.key).open('a', encoding='utf-8') as f:
            f.write('Проверить согласованный объём.')
        request = request_revision(self.vault, self.key)
        request.pop('estimate')
        for kind in ('effort', 'risks'):
            path = self.paths[kind]
            name = path.relative_to(self.vault.path).as_posix()
            old = f'# Ранее согласованный раздел {kind}\r\n'
            before = b'\xef\xbb\xbf' + old.encode()
            request['documents'][name] = dict(text=old, sha256=hashlib.sha256(before).hexdigest())
            path.write_bytes(before)
        with self.vault.locked() as db:
            db.execute('UPDATE revision_requests SET document=? WHERE id=?',
                       (json.dumps(request), request['id']))
        ensure_workspace(self.vault, self.key)
        self.assertIn('совпадают', compare_revision(self.vault, request['id']))
        self.assertEqual(request_revision(self.vault, self.key)['id'], request['id'])
        self.replace('risks', 'Ранее согласованный', 'Изменённый')
        self.assertIn('Изменённый', compare_revision(self.vault, request['id']))
        with self.assertRaisesRegex(ValueError, 'изменились'):
            accept_revision(self.vault, self.key, request['id'], 'Готово', 'Сверка')
