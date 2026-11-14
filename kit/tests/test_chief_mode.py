"""Переключение профиля проекта с сохранением обеих версий инструкций."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit import chief_mode, install_chief_skill, installer, update_project
from kit.run_opencode import HEADER
from kit.project_vault import ProjectVault
from kit.task_control_state import operation_lock


class ChiefModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        self.source = home / 'release'
        self.project = home / 'application'
        for name in (*installer.ROOT_FILES, 'kit/run_opencode.py', 'kit/prompts/master-chief.md',
                     'docs/guide.md', '.opencode/commands/module.md'):
            self.write(self.source, name, 'поставка ' + name)

    @staticmethod
    def write(root, name, value):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode('utf-8'))
        return target

    def install(self):
        installer.install_project(self.source, str(self.project))

    def switch(self, enabled):
        return chief_mode.set_mode(self.project, enabled)

    def test_ordinary_user_files_survive_install_on_off_and_two_rounds(self):
        touched = ['AGENTS.md','opencode.jsonc','.opencode/commands/module.md']
        untouched = ['.opencode/commands/private.md','.opencode/opencode.jsonc','.opencode/kit-models.json',
                     '.claude/skills/other/SKILL.md', 'CLAUDE.md', 'README.md']
        for name in touched + untouched:
            self.write(self.project, name, 'мои правила')
        self.install()
        self.assertEqual(chief_mode.mode_state(self.project), dict(status='off', enabled=False, explicit=True))
        for _ in range(2):
            self.assertTrue(self.switch(True)['enabled'])
            for name in touched:
                self.assertEqual((self.project/name).read_text(encoding='utf-8'), 'поставка ' + name)
            self.write(self.project,'AGENTS.md','настройки Чифа')
            self.assertTrue(self.switch(True)['enabled'])
            self.assertFalse(self.switch(False)['enabled'])
            for name in touched + untouched:
                self.assertEqual((self.project/name).read_text(encoding='utf-8'), 'мои правила')
            # Пользователь может сменить обе версии отдельно.
            self.switch(True)
            self.assertEqual((self.project/'AGENTS.md').read_text(encoding='utf-8'), 'настройки Чифа')
            self.write(self.project,'AGENTS.md','поставка AGENTS.md')
            self.switch(False)

    def test_skills_and_overlay_follow_mode_and_off_update_goes_to_parked_copy(self):
        self.install()
        for agent in ('claude','codex','pi','opencode'):
            target = install_chief_skill.install_chief_skill(self.project,agent)
            self.assertTrue(target.is_relative_to(self.project / chief_mode.DIRECTORY))
        self.write(self.source,'AGENTS.md','второй выпуск')
        self.write(self.source,'.opencode/commands/second.md','вторая команда')
        update_project.update_project(self.source,str(self.project),apply=True)
        self.assertFalse((self.project/'AGENTS.md').exists())
        self.switch(True)
        self.assertEqual((self.project/'AGENTS.md').read_text(encoding='utf-8'), 'второй выпуск')
        for name in chief_mode.SKILLS:
            self.assertTrue((self.project/name).exists())
        overlay = self.write(self.project,chief_mode.GENERATED,HEADER + '{}')
        self.switch(False)
        self.assertFalse(overlay.exists())
        for name in (*chief_mode.SKILLS,'.opencode/commands/second.md'):
            self.assertFalse((self.project/name).exists())
        self.switch(True)
        self.assertEqual(overlay.read_text(encoding='utf-8'), HEADER + '{}')

    def test_interrupted_transition_blocks_work_and_recovers_without_overwrite(self):
        self.write(self.project,'AGENTS.md','личные инструкции')
        self.install()
        real = chief_mode._save
        def crash(root,state):
            if any(step['phase']=='restore' for step in state.get('moves',{}).values()):
                raise OSError('питание отключено')
            return real(root,state)
        with patch.object(chief_mode,'_save',side_effect=crash), self.assertRaises(OSError):
            self.switch(True)
        with self.assertRaises(ValueError):
            chief_mode.require_enabled(self.project)
        self.assertEqual(chief_mode.mode_state(self.project)['status'],'enabling')
        self.switch(True)
        self.switch(False)
        self.assertEqual((self.project/'AGENTS.md').read_text(encoding='utf-8'),'личные инструкции')

    def test_alternative_opencode_configs_release_the_slot_only_until_off(self):
        originals = {}
        for name in chief_mode.OPENCODE_ALTERNATES:
            originals[name] = self.write(self.project, name, '// настройки ' + name + '\r\n{}').read_bytes()
        self.install()
        for cycle in range(2):
            self.switch(True)
            self.assertFalse((self.project/'opencode.json').exists())
            self.assertFalse((self.project/'.opencode/opencode.json').exists())
            overlay = self.project/chief_mode.GENERATED
            if cycle == 0:
                self.assertFalse(overlay.exists())
                overlay.write_text(HEADER + '{}', encoding='utf-8')
            else:
                self.assertEqual(overlay.read_text(encoding='utf-8'), HEADER + '{}')
            self.switch(False)
            for name, original in originals.items():
                self.assertEqual((self.project/name).read_bytes(), original)

    def test_failed_pause_and_lock_refuse_switch_then_retry(self):
        self.install()
        self.switch(True)
        with operation_lock(self.project,'chief-mode-switch'), self.assertRaises(ValueError):
            self.switch(False)
        with patch('kit.native_team.pause_project',side_effect=ValueError('Остановка не подтверждена')), self.assertRaises(ValueError):
            self.switch(False)
        self.assertEqual(chief_mode.mode_state(self.project)['status'],'disabling')
        self.assertTrue((self.project/'AGENTS.md').exists())
        self.switch(False)
        self.assertFalse((self.project/'AGENTS.md').exists())

    def test_ordinary_project_is_off_and_previous_installation_starts_on(self):
        self.project.mkdir(parents=True)
        self.assertFalse(chief_mode.mode_state(self.project)['explicit'])
        self.assertFalse(chief_mode.mode_state(self.project)['enabled'])
        with self.assertRaisesRegex(ValueError,'установите'):
            self.switch(True)
        self.install()
        self.switch(True)
        (self.project/chief_mode.STATE).unlink()
        self.assertEqual(chief_mode.mode_state(self.project),dict(status='on', enabled=True, explicit=False))
        self.switch(False)
        self.assertFalse((self.project/'AGENTS.md').exists())

    def test_legacy_vault_opts_in_without_taking_ownership_of_user_instructions(self):
        rules = self.write(self.project, 'AGENTS.md', 'свои инструкции')
        ProjectVault.connect(self.project, self.project.parent/'база')
        self.assertEqual(chief_mode.mode_state(self.project),dict(status='on',enabled=True,explicit=False))
        self.assertFalse(self.switch(False)['enabled'])
        self.assertTrue(self.switch(True)['enabled'])
        self.assertEqual(rules.read_text(encoding='utf-8'), 'свои инструкции')
