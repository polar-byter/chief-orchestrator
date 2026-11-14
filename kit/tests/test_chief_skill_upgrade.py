"""Обновление проектной инструкции и сохранность пользовательских редакций."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kit import install_chief_skill as skill
from kit.installer import MANIFEST


class SkillUpgradeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.source = self.base / 'поставка.md'
        self.current = skill.SOURCE.read_bytes()
        self.source.write_bytes(self.current)
        replacement = patch.object(skill, 'SOURCE', self.source)
        replacement.start()
        self.addCleanup(replacement.stop)

    def project(self, agent):
        root = self.base / agent
        prompt = root / 'kit/prompts/master-chief.md'
        prompt.parent.mkdir(parents=True)
        prompt.touch()
        return root

    def test_all_agents_install_upgrade_repeat_preserve_edits_and_offer_file(self):
        for agent, folder in skill.AGENT_FOLDERS.items():
            with self.subTest(agent=agent):
                root = self.project(agent)
                previous = b'---\nname: chief-orchestrator\n---\nDistributed previous version.\n'
                self.source.write_bytes(previous)
                target = skill.install_chief_skill(root, agent)
                self.assertEqual(target, root / folder / skill.SKILL_FILE)
                self.assertEqual(target.read_bytes(), previous)
                self.source.write_bytes(self.current)
                self.assertEqual(skill.install_chief_skill(root, agent), target)
                self.assertEqual(target.read_bytes(), self.current)
                marker, = (root / '.metrics').glob('chief-skill-*.json')
                hash_value = json.loads(marker.read_bytes())['files'][target.relative_to(root).as_posix()]
                self.assertEqual(hash_value, hashlib.sha256(self.current).hexdigest())
                times = [p.stat().st_mtime_ns for p in (target, marker)]
                skill.install_chief_skill(root, agent)
                self.assertEqual([p.stat().st_mtime_ns for p in (target, marker)], times)
                target.write_bytes(b'My changes')
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'Новая поставка для сравнения'):
                        skill.install_chief_skill(root, agent)
                self.assertEqual(target.read_bytes(), b'My changes')
                offered, = target.parent.glob('*.new')
                self.assertEqual(offered.read_bytes(), self.current)

    def test_previous_install_manifest_recognises_only_matching_checksum(self):
        root = self.project('codex')
        target = root / '.agents' / skill.SKILL_FILE
        target.parent.mkdir(parents=True)
        previous = b'release previously tracked by install_project'
        target.write_bytes(previous)
        manifest = root / MANIFEST
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps(dict(format=1, files={
            (Path('kit') / skill.SKILL_FILE).as_posix(): hashlib.sha256(previous).hexdigest()})), encoding='utf-8')
        skill.install_chief_skill(root, 'codex')
        self.assertEqual(target.read_bytes(), self.current)
        target.write_bytes(previous + b'custom')
        candidate = target.with_name('SKILL.md.new')
        candidate.write_bytes(b'older comparison with notes')
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, 'Новая поставка для сравнения'):
                skill.install_chief_skill(root, 'codex')
        self.assertEqual(target.read_bytes(), previous + b'custom')
        self.assertEqual(candidate.read_bytes(), b'older comparison with notes')
        other, = (file for file in target.parent.glob('*.new') if file != candidate)
        self.assertEqual(other.read_bytes(), self.current)

    def test_interruption_after_replacing_skill_repairs_marker_without_rewriting_skill(self):
        root = self.project('claude')
        self.source.write_bytes(b'first distributed copy')
        target = skill.install_chief_skill(root, 'claude')
        marker, = (root / '.metrics').glob('chief-skill-*.json')
        previous_marker = marker.read_bytes()
        original = skill._replace_if_unchanged
        def interrupt(path, before, after):
            if path == marker:
                raise OSError('прерывание обновления метки')
            original(path, before, after)
        self.source.write_bytes(self.current)
        with patch.object(skill, '_replace_if_unchanged', side_effect=interrupt), self.assertRaises(OSError):
            skill.install_chief_skill(root, 'claude')
        self.assertEqual(target.read_bytes(), self.current)
        self.assertEqual(marker.read_bytes(), previous_marker)
        changed_at = target.stat().st_mtime_ns
        skill.install_chief_skill(root, 'claude')
        self.assertNotEqual(marker.read_bytes(), previous_marker)
        self.assertEqual(target.stat().st_mtime_ns, changed_at)

    def test_unknown_files_and_one_character_legacy_edit_are_preserved(self):
        root = self.project('opencode')
        target = root / '.agents' / skill.SKILL_FILE
        target.parent.mkdir(parents=True)
        target.write_bytes(self.current + b'!')
        with self.assertRaisesRegex(ValueError, 'Сохранена ваша инструкция'):
            skill.install_chief_skill(root, 'opencode')
        self.assertEqual(target.read_bytes(), self.current + b'!')
        self.assertEqual(target.with_name('SKILL.md.new').read_bytes(), self.current)
