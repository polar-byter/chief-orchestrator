"""Обновление действующей установки с локальными правками, конфликтами и прерыванием."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kit import installer, update_project as updater
from kit.chief_mode import set_mode
from kit.task_control_state import operation_lock


class UpdateProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.source = base / 'release'
        self.project = base / 'Приложение'
        for name in (*installer.ROOT_FILES, 'kit/run_opencode.py', 'kit/worker.py', 'docs/manual.md',
                     '.opencode/commands/module.md'):
            self.write(self.source, name, 'первая поставка')
        installer.install_project(self.source, str(self.project))
        set_mode(self.project, True)

    @staticmethod
    def write(root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def update(self, apply=False):
        return updater.update_project(self.source, str(self.project), apply=apply)

    def test_pi_migration_preserves_old_manifest_and_checks_conflicts_before_adoption(self):
        project = self.project.parent / 'Рабочий Pi'
        files = {'pi-kit.json':'{"runtime":"pi","preferences":[1]}', 'kit/run_pi.py':'old',
                 'AGENTS.md':'first', 'kit/worker.py':'original'}
        for name, content in files.items():
            self.write(project, name, content)
        self.write(self.source, 'kit/skills/chief-orchestrator/SKILL.md', 'skill')
        old = dict(format=1, runtime='pi', files={n:hashlib.sha256(s.encode()).hexdigest() for n,s in files.items()})
        manifest = self.write(project, installer.PI_MANIFEST, json.dumps(old))
        self.write(project, 'kit/worker.py', 'local changes')
        self.write(project, '.pi/kit-models.json', 'chosen models')
        result = updater.update_project(self.source, str(project), apply=True)
        self.assertEqual(result['status'], 'conflicts')
        self.assertFalse((project / installer.MANIFEST).exists())
        self.write(project, 'kit/worker.py', 'original')
        result = updater.update_project(self.source, str(project), apply=True)
        self.assertEqual((result['status'], result['agent'], result['migration']), ('applied', 'pi', True))
        self.assertEqual(json.loads(manifest.read_bytes()), old)
        self.assertEqual((project/'pi-kit.json').read_text(), files['pi-kit.json'])
        self.assertEqual((project/'.pi/kit-models.json').read_text(), 'chosen models')
        repeat = updater.update_project(self.source, str(project), apply=True)
        self.assertEqual((repeat['update'], repeat['add']), ([], []))

    def manifest(self):
        return json.loads((self.project / installer.MANIFEST).read_bytes())

    def test_preview_preserves_choices_and_only_updates_owned_files(self):
        for name in ('opencode.jsonc', 'AGENTS.md', 'docs/manual.md', 'kit/worker.py'):
            self.write(self.source, name, 'вторая поставка')
        user_files = {'README.md':'рабочий README', 'AGENTS.md':'правки оператора', '.metrics/example/events.jsonl':'журнал',
                      '.opencode/opencode.jsonc':'локальный overlay', '.opencode/kit-models.json':'выбранные модели',
                      'pi-kit.json':'параметры Pi', 'kit/code.py':'занято', 'src/App.java':'исходник'}
        for name, content in user_files.items():
            self.write(self.project, name, content)
        self.write(self.source, 'kit/code.py', 'другое содержимое')
        self.write(self.source, 'kit/module.py', 'добавленный модуль')
        (self.source / '.opencode/commands/module.md').unlink()
        (self.project / 'kit/worker.py').unlink()
        baseline = self.manifest()
        snapshot = lambda: {p.relative_to(self.project): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}
        before = snapshot()
        plan = self.update()
        self.assertEqual(before, snapshot())
        self.assertEqual(plan['update'], ['docs/manual.md'])
        self.assertEqual(plan['add'], ['kit/module.py'])
        expected_conflicts = {'kit/code.py','AGENTS.md','kit/worker.py'}
        self.assertEqual({x['path'] for x in plan['conflicts']}, expected_conflicts)
        self.assertEqual(plan['status'], 'plan')
        result = self.update(True)
        self.assertEqual(result['status'], 'conflicts')
        self.assertEqual((self.project / 'docs/manual.md').read_text(encoding='utf-8'), 'вторая поставка')
        self.assertTrue((self.project / 'kit/module.py').is_file())
        self.assertTrue((self.project / '.opencode/commands/module.md').is_file())
        self.assertFalse((self.project / 'kit/worker.py').exists())
        self.assertEqual((self.project / 'opencode.jsonc').read_text(encoding='utf-8'), 'первая поставка')
        for name, content in user_files.items():
            self.assertEqual((self.project / name).read_text(encoding='utf-8'), content)
        owned = self.manifest()
        self.assertEqual(owned['installed_at'], baseline['installed_at'])
        self.assertEqual(owned['files']['AGENTS.md'], baseline['files']['AGENTS.md'])
        self.assertNotIn('kit/code.py', owned['files'])
        self.assertEqual(self.update()['update'], [])
        self.assertEqual(self.update()['add'], [])

    def test_recovery_after_unpublished_manifest_and_changed_release(self):
        self.write(self.source, 'docs/manual.md', 'изменение')
        self.write(self.source, 'kit/new.py', 'новый модуль')
        original = updater._replace

        def interrupt(path, *args):
            if path == self.project / installer.MANIFEST:
                raise OSError('имитация остановки процесса')
            return original(path, *args)

        with patch.object(updater, '_replace', side_effect=interrupt), self.assertRaisesRegex(OSError, 'остановки'):
            self.update(True)
        self.assertEqual(self.update()['status'], 'interrupted')
        self.assertEqual((self.project / 'kit/new.py').read_text(encoding='utf-8'), 'новый модуль')
        self.write(self.source, 'docs/manual.md', 'отличающаяся сборка')
        with self.assertRaisesRegex(installer.InstallError, 'та же поставка'):
            self.update(True)
        self.write(self.source, 'docs/manual.md', 'изменение')
        result = self.update(True)
        self.assertTrue(result['resumed'])
        self.assertFalse((self.project / updater.JOURNAL).exists())
        self.assertIn('kit/new.py', self.manifest()['files'])
        self.assertFalse(self.update()['conflicts'])

    def test_new_file_race_preserves_concurrent_content(self):
        self.write(self.source, 'kit/new.py', 'из поставки')
        publish = updater.publish_new

        def occupy(path, data):
            if path == self.project / 'kit/new.py':
                self.write(self.project, 'kit/new.py', 'создано оператором')
            return publish(path, data)

        with patch.object(updater, 'publish_new', side_effect=occupy), self.assertRaisesRegex(installer.InstallError, 'занят'):
            self.update(True)
        with self.assertRaisesRegex(installer.InstallError, 'изменён'):
            self.update(True)
        self.assertEqual((self.project / 'kit/new.py').read_text(encoding='utf8'), 'создано оператором')
        self.assertNotIn('kit/new.py', self.manifest()['files'])
        self.assertEqual(self.update()['status'], 'interrupted')

    def test_external_edit_before_manifest_is_preserved_and_recovery_finishes_after_reconciliation(self):
        self.write(self.source, 'docs/manual.md', 'новая поставка')
        original = updater._replace
        def external_edit(path, *args):
            original(path, *args)
            if path == self.project / 'docs/manual.md':
                self.write(self.project,'docs/manual.md','новая локальная правка')
        with patch.object(updater, '_replace', side_effect=external_edit), self.assertRaisesRegex(installer.InstallError, 'манифеста'):
            self.update(True)
        self.assertEqual((self.project / 'docs/manual.md').read_text(encoding='utf8'), 'новая локальная правка')
        self.assertEqual(self.update()['status'], 'interrupted')
        self.write(self.project, 'docs/manual.md', 'новая поставка')
        self.assertTrue(self.update(True)['resumed'])

    def test_reopens_without_update_and_adopts_matching_bytes_only_for_managed_file(self):
        self.write(self.source,'kit/worker.py','новое содержимое')
        self.write(self.project,'kit/worker.py','новое содержимое')
        original = self.manifest()
        self.assertTrue(installer.install_project(self.source,str(self.project))['reused'])
        self.assertEqual(self.manifest(), original)
        self.assertEqual(self.update(True)['status'], 'applied')
        self.assertNotEqual(self.manifest()['files']['kit/worker.py'], original['files']['kit/worker.py'])
        self.assertEqual(self.update()['update'], [])

    def test_update_requires_separate_installed_folder_and_existing_lock_is_respected(self):
        empty = self.project.parent / 'empty'
        empty.mkdir()
        for destination in (self.source, self.source / 'subdir', self.project.parent, empty):
            with self.subTest(path=destination), self.assertRaises(installer.InstallError):
                updater.update_project(self.source,str(destination),apply=True)
        self.assertFalse(list(empty.iterdir()))
        self.write(self.source,'kit/worker.py','новая версия')
        with operation_lock(self.project, 'kit-distribution-update'), self.assertRaises(ValueError):
            self.update(True)
        self.assertFalse((self.project / updater.JOURNAL).exists())

    def test_unknown_manifest_paths_cannot_introduce_targets_outside_distribution(self):
        path = self.project / installer.MANIFEST
        manifest = self.manifest()
        manifest['files']['../unmanaged'] = '0'*64
        path.write_text(json.dumps(manifest), encoding='utf8')
        marker = self.write(self.project.parent,'unmanaged','сохранить')
        self.write(self.source,'kit/worker.py','новая версия')
        result = self.update(True)
        self.assertIn('../unmanaged', result['retired'])
        self.assertEqual(marker.read_text(encoding='utf8'), 'сохранить')

    def test_real_previous_installation_updates_to_current_distribution(self):
        release = Path(__file__).resolve().parents[2]
        self.write(self.project, 'README.md', 'Программа заказчика')
        result = updater.update_project(release, str(self.project), apply=True)
        self.assertEqual(result['status'], 'applied')
        self.assertIn('kit/update_project.py', result['add'])
        self.assertEqual((self.project / 'kit/update_project.py').read_bytes(), Path(updater.__file__).read_bytes())
        self.assertEqual((self.project / 'README.md').read_text(encoding='utf8'), 'Программа заказчика')
        self.assertTrue(installer.install_project(release, str(self.project))['reused'])

if __name__ == '__main__':
    unittest.main()
