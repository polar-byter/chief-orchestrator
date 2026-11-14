"""Проверки подготовки Obsidian и подтверждения активной заметки."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kit import obsidian_app as obsidian


class ObsidianAppTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.vault = self.root / 'Документация'
        self.vault.mkdir()
        self.note = self.vault / 'Этап 1.md'
        self.note.write_text('# Этап', encoding='utf-8')

    def test_existing_installation_does_not_download(self):
        with patch.object(obsidian, 'find_application', return_value=self.root / 'Obsidian.exe') as find, \
             patch.object(obsidian, 'download_package') as download:
            self.assertEqual(obsidian.ensure_application(), find.return_value)
            download.assert_not_called()

    def test_download_rejection_precedes_execution_and_can_be_retried(self):
        candidate = self.root / 'Obsidian.exe'
        with patch.object(obsidian.sys, 'platform', 'win32'), \
             patch.object(obsidian, 'application_home', return_value=self.root), \
             patch.object(obsidian, 'find_application', side_effect=[None, None, None, None, candidate]), \
             patch.object(obsidian, 'urlopen', return_value=io.BytesIO(b'wrong')), \
             patch.object(obsidian.subprocess, 'run') as installer:
            with self.assertRaisesRegex(ValueError, 'сумма'):
                obsidian.ensure_application()
            installer.assert_not_called()
            with patch.object(obsidian, 'download_package'):
                installer.return_value.returncode = 0
                self.assertEqual(obsidian.ensure_application(), candidate)
            self.assertEqual(installer.call_args.args[0][1:], ['/currentuser', '/S'])

    def test_installer_error_reports_its_exit_code(self):
        with patch.object(obsidian.sys, 'platform', 'win32'), \
             patch.object(obsidian, 'find_application', return_value=None), \
             patch.object(obsidian, 'application_home', return_value=self.root), \
             patch.object(obsidian, 'download_package'), \
             patch.object(obsidian.subprocess, 'run') as installer:
            installer.return_value.returncode = 2
            with self.assertRaisesRegex(ValueError, 'кодом 2'):
                obsidian.ensure_application()

    def test_linux_install_publishes_verified_executable(self):
        data = b'appimage fixture'
        assets = {'aarch64': ('Obsidian-arm64.AppImage', hashlib.sha256(data).hexdigest())}
        installed = self.root / 'Obsidian.AppImage'
        with patch.object(obsidian.sys, 'platform', 'linux'), \
             patch.object(obsidian, 'find_application', side_effect=[None, None, installed]), \
             patch.object(obsidian, 'application_home', return_value=self.root), \
             patch.object(obsidian.platform, 'machine', return_value='aarch64'), \
             patch.dict(obsidian.PACKAGES, assets), \
             patch.object(obsidian, 'urlopen', return_value=io.BytesIO(data)) as request:
            self.assertEqual(obsidian.ensure_application(), installed)
            self.assertEqual(installed.read_bytes(), data)
            self.assertEqual(request.call_args.args, (obsidian.RELEASE + 'Obsidian-arm64.AppImage',))
            if os.name != 'nt':
                self.assertTrue(os.access(installed, os.X_OK))

    def test_invalid_or_foreign_note_does_not_install(self):
        for invalid in (self.root / 'outside.md', Path('absent.md'), Path('../outside.md')):
            with self.subTest(invalid=invalid), patch.object(obsidian, 'ensure_application') as install:
                with self.assertRaises(ValueError):
                    obsidian.open_note(self.vault, invalid)
                install.assert_not_called()

    def test_another_installation_is_reported_without_executing_a_second_installer(self):
        with obsidian.installation_lock(self.root), patch.object(obsidian, 'find_application', return_value=None), \
             patch.object(obsidian, 'application_home', return_value=self.root), \
             patch.object(obsidian, 'install_application') as install:
            with self.assertRaisesRegex(ValueError, 'уже устанавливает'):
                obsidian.ensure_application()
            install.assert_not_called()

    def test_profile_isolated_by_vault_and_reopen_preserves_preferences(self):
        home = self.root / 'profiles'
        profile = obsidian.prepare_profile(self.vault, home)
        registry = profile / 'obsidian.json'
        original = json.loads(registry.read_text(encoding='utf-8'))
        original['language'] = 'ru'
        registry.write_text(json.dumps(original), encoding='utf-8')
        other = self.root / 'Другая база'
        other.mkdir()
        self.assertNotEqual(profile, obsidian.prepare_profile(other, home))
        self.assertEqual(profile, obsidian.prepare_profile(self.vault, home))
        self.assertEqual(json.loads(registry.read_text(encoding='utf-8')), original)

    def test_registry_mismatch_is_not_repaired_by_overwriting(self):
        home = self.root / 'profiles'
        profile = obsidian.prepare_profile(self.vault, home)
        registry = profile / 'obsidian.json'
        registry.write_text('{"vaults": {}}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'другому'):
            obsidian.prepare_profile(self.vault, home)
        self.assertEqual(registry.read_text(encoding='utf-8'), '{"vaults": {}}')

    def test_active_note_ignores_an_inactive_editor_and_sidebar(self):
        workspace = self.root / 'workspace.json'
        def leaf(identifier, kind, path):
            return {'id': identifier, 'type': 'leaf', 'state': {'type': kind, 'state': {'file': path}}}
        state = {'main': {'children': [leaf('a', 'markdown', 'first.md'), leaf('b', 'markdown', 'second.md')]},
                 'left': {'children': [leaf('c', 'backlinks', 'first.md')]}, 'active': 'b'}
        workspace.write_text(json.dumps(state))
        self.assertEqual(obsidian.active_note(workspace)[0], 'second.md')
        state['active'] = 'c'
        workspace.write_text(json.dumps(state))
        self.assertIsNone(obsidian.active_note(workspace))

    def test_old_workspace_and_launcher_exit_do_not_confirm_an_open(self):
        with patch.object(obsidian.sys, 'platform', 'win32'), \
             patch.object(obsidian, 'active_note', return_value=(self.note.name, 1)), \
             patch.object(obsidian.time, 'monotonic', side_effect=[0, 1, 40]), \
             patch.object(obsidian.time, 'sleep'), patch.object(obsidian.subprocess, 'Popen') as popen:
            popen.return_value.poll.return_value = 0
            with self.assertRaisesRegex(ValueError, 'не подтвердил'):
                obsidian.wait_for_note(self.root / 'Obsidian.exe', self.root, self.note, self.vault, [], None)


if __name__ == '__main__':
    unittest.main()
