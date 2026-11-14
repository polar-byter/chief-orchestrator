"""Проверки границы между настройкой проекта и сохранённой задачей."""
import json
from pathlib import Path
import tempfile
import unittest

from kit.dashboard.controls import TaskControls
from kit.dashboard.session_control import SessionControl
from kit.execution_controller import ExecutionController
from kit.install_chief_skill import install_chief_skill
from kit.run_pi import main as pi_main
from kit.runtimes.pi_gateway import PiSessionControl
from kit.task_metrics import TaskMetrics


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        project = tempfile.TemporaryDirectory()
        self.addCleanup(project.cleanup)
        self.root = Path(project.name)
        self.selection = self.root / 'pi-kit.json'

    def test_controller_and_dashboard_follow_project_setting(self):
        controls = TaskControls(self.root)
        for selection, expected in ((None, SessionControl), ('pi', PiSessionControl),
                                    ('opencode', SessionControl)):
            if selection:
                self.selection.write_text(json.dumps({'runtime': selection}), encoding='utf-8')
            with self.subTest(selection=selection):
                self.assertIsInstance(controls.gateway, expected)
                self.assertIsInstance(ExecutionController(self.root, 'demo').service, expected)
        self.assertFalse((self.root / '.pi/kit-service').exists())

    def test_changing_runtime_blocks_actions_on_existing_task(self):
        metrics = TaskMetrics(self.root)
        metrics.start('legacy', 'ses_root')
        self.selection.write_text('{"runtime":"pi"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'opencode'):
            TaskControls(self.root).context('legacy', 'pause')
        with self.assertRaisesRegex(ValueError, 'opencode'):
            metrics.read_events_if_work_allowed('legacy')
        self.assertEqual(len(metrics.read('legacy')), 1)

    def test_invalid_runtime_does_not_reserve_task_id(self):
        self.selection.write_text('{"runtime":"unknown"}', encoding='utf-8')
        metrics = TaskMetrics(self.root)
        with self.assertRaises(ValueError):
            metrics.start('task')
        self.selection.write_text('{"runtime":"pi"}', encoding='utf-8')
        self.assertEqual(metrics.start('task')['runtime'], 'pi')

    def test_pi_configuration_preserves_saved_roles(self):
        self.selection.write_text('{"runtime":"pi","defaults":{}}', encoding='utf-8')
        arguments = ['--root', str(self.root), '--configure']
        self.assertEqual(pi_main(arguments), 0)
        choices = self.root / '.pi/kit-models.json'
        saved = '{"masterChief":{"source":"pi","model":"test/chief"}}'
        choices.write_text(saved, encoding='utf-8')
        self.assertEqual(pi_main(arguments), 0)
        self.assertEqual(choices.read_text(encoding='utf-8'), saved)
        self.assertFalse((self.root / '.opencode').exists())

    def test_skill_installation_uses_one_source_and_keeps_local_edits(self):
        prompt = self.root / 'kit/prompts/master-chief.md'
        prompt.parent.mkdir(parents=True)
        prompt.touch()
        shared = install_chief_skill(self.root, 'codex')
        for agent in ('pi', 'opencode'):
            self.assertEqual(install_chief_skill(self.root, agent), shared)
        claude = install_chief_skill(self.root, 'claude')
        self.assertNotEqual(claude, shared)
        self.assertEqual(claude.read_bytes(), shared.read_bytes())
        shared.write_text('Местная редакция', encoding='utf-8')
        with self.assertRaises(ValueError):
            install_chief_skill(self.root, 'codex')
        self.assertEqual(shared.read_text(encoding='utf-8'), 'Местная редакция')
