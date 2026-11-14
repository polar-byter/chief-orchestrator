"""Создание, перенос и сохранность проектной базы с обычными Markdown-заметками."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit import project_vault
from kit.project_vault import BINDING_FILE, PROJECT_FILE, ProjectVault


class ProjectVaultTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'код'
        self.root.mkdir()

    def test_reopen_after_move_retains_identity_and_manual_edits(self):
        vault = ProjectVault.connect(self.root, name='Книга заказов')
        self.assertEqual(ProjectVault.load(self.root), vault)
        self.assertEqual(vault, ProjectVault.connect(self.root))
        home = vault.home.read_text(encoding='utf-8') + '\nЗаметка руководителя.\n'
        vault.home.write_text(home, encoding='utf-8')
        card = vault.add('stage', 'stage-2', 'Анализ требований')
        with card.open('a', encoding='utf-8') as note:
            note.write('Решение аналитика')
        moved = self.base / 'Перенесённая база'
        vault.path.rename(moved)
        with self.assertRaisesRegex(ValueError, 'отсутствует|не найдена'):
            ProjectVault.load(self.root)
        with self.assertRaisesRegex(ValueError, 'не найдена'):
            ProjectVault.connect(self.root)
        self.assertFalse(vault.path.exists())
        connected = ProjectVault.connect(self.root, moved)
        self.assertEqual(connected.project_id, vault.project_id)
        self.assertEqual(connected.home.read_text(encoding='utf-8'), home)
        self.assertEqual(ProjectVault.load(self.root).path, moved)
        with self.assertRaisesRegex(ValueError, 'Привязка проекта изменилась'):
            vault.add('stage', 'old', 'Устаревший объект')
        self.assertIn('Решение аналитика', connected.add('stage', 'stage-2', 'Анализ требований').read_text(encoding='utf-8'))

    def test_project_pages_and_distinct_records_are_navigable(self):
        vault = ProjectVault.connect(self.root)
        stage = vault.add('stage', 'phase', 'Сбор платежей')
        feature = vault.add('feature', 'payment', 'Приём платежа', 'phase', 'Клиент оплачивает заказ.')
        self.assertEqual([item['key'] for item in vault.records()], ['phase', 'payment'])
        self.assertIn('path:"Этапы/"', vault.home.read_text(encoding='utf-8'))
        self.assertIn('path:"Возможности/phase/"', stage.read_text(encoding='utf-8'))
        self.assertIn(feature.stem, vault.workspace('payment').stem)
        text = feature.read_text(encoding='utf-8')
        self.assertIn('[[Рабочее место/' + vault.workspace('payment').stem + '|Доработать фичу]]', text)
        self.assertIn('[[Этапы/' + stage.stem + '|Открыть этап]]', text)
        self.assertIn('## Что изменить', vault.workspace('payment').read_text(encoding='utf-8'))
        self.assertTrue((vault.path / 'История/payment/Редакции.md').is_file())
        with self.assertRaises(ValueError):
            vault.add('feature', 'another', 'Другой платёж', 'payment')
        with self.assertRaises(ValueError):
            vault.add('stage', 'PHASE', 'Другое название')
        with self.assertRaises(ValueError):
            vault.add('feature', 'payment', 'Изменение оригинала', 'phase')
        self.assertEqual(vault.record('payment')['title'], 'Приём платежа')

    def test_another_project_cannot_read_or_adopt_vault(self):
        vault = ProjectVault.connect(self.root)
        other_root = self.base / 'другой проект'
        other_root.mkdir()
        with self.assertRaises(ValueError):
            ProjectVault.connect(other_root, vault.path)
        self.assertFalse((other_root / PROJECT_FILE).exists())
        other = ProjectVault.connect(other_root)
        vault.add('stage', 'same', 'Результат А')
        other.add('stage', 'same', 'Результат Б')
        with self.assertRaises(ValueError):
            ProjectVault.connect(other_root, vault.path)
        self.assertEqual(other.record('same')['title'], 'Результат Б')
        self.assertEqual(vault.record('same')['title'], 'Результат А')
        (other.root / BINDING_FILE).write_text((vault.root / BINDING_FILE).read_text(encoding='utf-8'), encoding='utf-8')
        with self.assertRaises(ValueError):
            ProjectVault.load(other.root)

    def test_existing_notes_and_overlapping_repository_are_rejected(self):
        notes = self.base / 'Личное'
        notes.mkdir()
        existing = notes / 'Документация проекта.md'
        existing.write_text('Личная запись', encoding='utf-8')
        for path in (self.root, self.base, notes):
            with self.subTest(path=path), self.assertRaises(ValueError):
                ProjectVault.connect(self.root, path)
        self.assertEqual(existing.read_text(encoding='utf-8'), 'Личная запись')
        self.assertFalse((notes / '.chief').exists())

    def test_recovery_after_failure_between_card_and_workspace(self):
        vault = ProjectVault.connect(self.root)
        actual_publish = project_vault.publish_new
        def fail_on_workspace(path, content):
            if path.parent.name == 'Рабочее место':
                raise OSError('Нет свободного места')
            return actual_publish(path, content)
        with patch.object(project_vault, 'publish_new', side_effect=fail_on_workspace):
            with self.assertRaisesRegex(OSError, 'свободного места'):
                vault.add('stage', 'scope', 'Границы работ')
        self.assertEqual(vault.records(), [])
        manual = (vault.path / 'Этапы/Границы работ (scope).md')
        old = manual.read_bytes()
        manual.write_bytes(b'my changes')
        with self.assertRaisesRegex(ValueError, 'Сохранена существующая'):
            vault.add('stage', 'scope', 'Границы работ')
        self.assertEqual(manual.read_bytes(), b'my changes')
        manual.write_bytes(old)
        self.assertEqual(vault.add('stage', 'scope', 'Границы работ'), manual)
        self.assertTrue(vault.workspace('scope').is_file())

    def test_parallel_add_is_idempotent_and_invalid_path_rejected(self):
        vault = ProjectVault.connect(self.root)
        with ThreadPoolExecutor(max_workers=3) as pool:
            cards = list(pool.map(lambda n: vault.add('stage', 'ready', 'Готовность'), range(3)))
        self.assertEqual(len(set(cards)), 1)
        self.assertEqual(len(vault.records()), 1)
        for key in ('../out', 'C:other', 'PRN', 'stage/one'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                vault.add('stage', key, 'Проверка ID')
        self.assertEqual(len(vault.records()), 1)
