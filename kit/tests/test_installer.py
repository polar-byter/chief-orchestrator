"""Installation boundaries, rollback, repeat setup and Desktop opening requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from kit import installer
from kit.chief_mode import mode_state, set_mode, staged_path


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.source = self.home / 'download'
        self.source.mkdir()
        for name in installer.ROOT_FILES:
            (self.source / name).write_text('template ' + name, encoding='utf-8')
        for name in ('kit/run_opencode.py', 'kit/prompts/master-chief.md',
                     '.opencode/commands/module.md', 'docs/guide.md'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('distributed source', encoding='utf-8')
        self.target = self.home / 'new parent' / 'Дом и бюджет'

    def install(self):
        return installer.install_project(self.source, str(self.target))

    def test_selected_cli_and_skill_survive_restart_and_mode_change(self):
        from kit.install_chief_skill import AGENT_FOLDERS, SKILL_FILE
        skill = self.source / 'kit' / SKILL_FILE
        skill.parent.mkdir(parents=True)
        skill.write_text('project skill', encoding='utf-8')
        for agent in ('pi', 'codex', 'claude'):
            with self.subTest(agent=agent):
                target = self.target / agent
                installer.install_project(self.source, str(target), agent=agent)
                self.assertEqual(installer.project_agent(target), agent)
                self.assertFalse(mode_state(target)['enabled'])
                entry = target / AGENT_FOLDERS[agent] / SKILL_FILE
                self.assertFalse(entry.exists())
                set_mode(target, True)
                self.assertEqual(entry.read_bytes(), skill.read_bytes())
                set_mode(target, False)
                self.assertFalse(entry.exists())

    def desktop(self):
        app = self.home / 'Desktop with spaces' / 'OpenCode.exe'
        app.parent.mkdir(exist_ok=True)
        app.touch()
        cli = app.parent / 'resources/opencode-cli.exe'
        cli.parent.mkdir(exist_ok=True)
        cli.touch()
        return installer.Desktop(app, cli)

    def test_new_project_creates_all_parents_and_records_only_owned_files(self):
        result = self.install()
        self.assertFalse(result['reused'])
        self.assertEqual((self.target / 'kit/run_opencode.py').read_text(), 'distributed source')
        manifest = json.loads((self.target / installer.MANIFEST).read_text())
        self.assertEqual(manifest['format'], 1)
        self.assertEqual(result['files'], len(manifest['files']))
        self.assertIn('.metrics/', (self.target / '.gitignore').read_text())
        self.assertNotIn(str(self.source), (self.target / installer.MANIFEST).read_text())

    def test_existing_application_and_requirements_are_preserved(self):
        self.target.mkdir(parents=True)
        for name in ('app.py', 'TASK.md', 'README.md'):
            (self.target / name).write_text('user content', encoding='utf-8')
        (self.target / '.gitignore').write_text('user-rule\n', encoding='utf-8')
        self.install()
        for name in ('app.py', 'TASK.md', 'README.md'):
            self.assertEqual((self.target / name).read_text(), 'user content')
        self.assertTrue((self.target / '.gitignore').read_text().startswith('user-rule\n'))

    def test_repeat_setup_reuses_project_and_preserves_saved_models(self):
        self.install()
        (self.target / '.opencode').mkdir(exist_ok=True)
        (self.target / '.opencode/kit-models.json').write_text('saved choice', encoding='utf-8')
        (self.source / 'opencode.jsonc').write_text('new release', encoding='utf-8')
        self.assertTrue(self.install()['reused'])
        self.assertEqual(staged_path(self.target, 'opencode.jsonc').read_text(), 'template opencode.jsonc')
        self.assertEqual((self.target / '.opencode/kit-models.json').read_text(), 'saved choice')

    def test_existing_configuration_is_rejected_before_any_changes(self):
        for name in ('kit', '.metrics/.chief-mode', 'docs/guide.md'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(dir=self.home) as directory:
                target = Path(directory)
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('do not overwrite', encoding='utf-8')
                before = {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()}
                with self.assertRaises(installer.InstallError):
                    installer.install_project(self.source, str(target))
                self.assertEqual(before, {str(p.relative_to(target)): p.read_bytes() for p in target.rglob('*') if p.is_file()})

    def test_only_distributable_files_are_copied(self):
        for name in ('.env', '.metrics/private.json', '.opencode/kit-models.json',
                     'kit/.env', 'kit/credentials.json', 'kit/__pycache__/secret.pyc'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private', encoding='utf-8')
        self.install()
        for name in ('.env', '.opencode/kit-models.json', 'kit/.env', 'kit/credentials.json', 'kit/__pycache__/secret.pyc'):
            self.assertFalse((self.target / name).exists(), name)
        self.assertFalse((self.target / '.metrics/private.json').exists())

    def test_invalid_folder_and_kit_locations_are_rejected(self):
        file = self.home / 'a-file'
        file.touch()
        for value in ('', 'relative/path', str(self.source), str(self.source / 'child'), str(self.home), str(file), self.target.anchor):
            with self.subTest(value=value), self.assertRaises(installer.InstallError):
                installer.install_project(self.source, value)

    def test_incomplete_download_does_not_create_a_project(self):
        (self.source / 'AGENTS.md').unlink()
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertFalse(self.target.exists())

    def test_copy_failure_removes_own_files_but_preserves_existing_data(self):
        self.target.mkdir(parents=True)
        (self.target / 'app.py').write_text('user source', encoding='utf-8')
        (self.target / '.gitignore').write_text('original rules', encoding='utf-8')
        original = Path.open
        def fail(path, mode='r', *args, **kwargs):
            if path == self.target / 'docs/guide.md' and mode == 'xb':
                raise PermissionError('simulated write failure')
            return original(path, mode, *args, **kwargs)
        with patch.object(Path, 'open', fail), self.assertRaises(PermissionError):
            self.install()
        self.assertEqual(set(p.name for p in self.target.iterdir()), {'app.py', '.gitignore'})
        self.assertEqual((self.target / '.gitignore').read_text(), 'original rules')

    def test_partial_write_failure_removes_partial_owned_file(self):
        self.target.mkdir(parents=True)
        (self.target / 'app.py').write_text('user source', encoding='utf-8')
        original = Path.open
        class BrokenStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def write(self, content):
                self.stream.write(content[:3])
                raise OSError('simulated partial write')
            def close(self): self.stream.close()
        def fail(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return BrokenStream(stream) if path == self.target / 'docs/guide.md' and mode == 'xb' else stream
        with patch.object(Path, 'open', fail), self.assertRaises(OSError):
            self.install()
        self.assertEqual([p.name for p in self.target.iterdir()], ['app.py'])

    def test_real_distribution_installs_and_prepares_without_model_generation(self):
        from kit import run_opencode
        source = Path(__file__).resolve().parents[2]
        result = installer.install_project(source, str(self.target))
        self.assertFalse(result['reused'])
        self.assertFalse(mode_state(self.target)['enabled'])
        set_mode(self.target, True)
        run_opencode.prepare(self.target)
        config = json.loads(run_opencode.strip_jsonc((self.target / '.opencode/opencode.jsonc').read_text(encoding='utf-8')))
        self.assertEqual(config['agents']['executor']['model'], 'opencode/mimo-v2.6-flash-free')
        base = json.loads(run_opencode.strip_jsonc((self.target / 'opencode.jsonc').read_text(encoding='utf-8')))
        self.assertEqual(config['agents']['build'].get('model', base['agents']['build']['model']), 'opencode/deepseek-v4.1-flash-free')
        self.assertTrue((self.target / 'docs/user/OPENCODE_KIT.en.md').is_file())
        self.assertIn('in this project root', (self.target / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertFalse((self.target / '.metrics/project-installer-20261016').exists())
        self.assertFalse((self.target / '.opencode/kit-models.json').exists())

    def test_terminal_cli_is_not_mistaken_for_desktop(self):
        cli = self.home / 'opencode-cli.exe'
        cli.touch()
        self.assertIsNone(installer.desktop_from_path(cli))
        self.assertEqual(installer.desktop_from_path(self.desktop().application), self.desktop())

    def test_linux_desktop_accepts_a_separate_cli_and_prefers_the_bundled_one(self):
        application = self.home / 'OpenCode' / 'ai.opencode.desktop'
        application.parent.mkdir()
        application.touch()
        with patch.object(installer.sys, 'platform', 'linux'), patch.object(installer.shutil, 'which') as which:
            which.return_value = None
            self.assertIsNone(installer.desktop_from_path(application))
            which.return_value = str(application)
            self.assertIsNone(installer.desktop_from_path(application))
            separate_cli = self.home / 'opencode'
            separate_cli.touch()
            which.return_value = str(separate_cli)
            self.assertEqual(installer.desktop_from_path(application), installer.Desktop(application, separate_cli))
            cli = application.parent / 'resources/opencode-cli'
            cli.parent.mkdir()
            cli.touch()
            self.assertEqual(installer.desktop_from_path(application), installer.Desktop(application, cli))

    def test_desktop_request_encodes_folder_and_never_sends_a_prompt(self):
        self.install()
        desktop = self.desktop()
        with patch.object(installer.subprocess, 'Popen', return_value=Mock()) as launch:
            installer.open_project(desktop, self.target)
        arguments = launch.call_args.args[0]
        self.assertEqual(arguments[0], str(desktop.application))
        self.assertIn('%', arguments[1])
        from urllib.parse import urlsplit, parse_qs
        self.assertEqual(parse_qs(urlsplit(arguments[1]).query)['directory'], [str(self.target)])
        self.assertEqual(launch.call_args.kwargs['cwd'], self.target)
        self.assertNotIn('prompt', ' '.join(arguments))

    def test_open_requires_prepared_project(self):
        self.install()
        set_mode(self.target, True)
        with patch.object(installer.subprocess, 'Popen') as launch:
            with self.assertRaises(installer.InstallError):
                installer.open_project(self.desktop(), self.target)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
