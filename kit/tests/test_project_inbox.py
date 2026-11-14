"""Доставка по флажку из обычной заметки Obsidian."""
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from kit import project_inbox as inbox
from kit.project_revisions import accept_revision, pending_requests, request_revision
from kit.project_vault import ProjectVault


class ProjectInboxTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        base = Path(folder.name)
        root = base / 'проект Чифа'
        root.mkdir()
        self.vault = ProjectVault.connect(root)
        self.vault.add('stage', 'plan', 'Планирование')
        self.work = self.vault.workspace('plan')
        self.template = self.work.read_text(encoding='utf-8')

    def mark(self, text, *, checked=True):
        template = self.template.replace('- [ ] Передать Чифу', '- [x] Передать Чифу') if checked else self.template
        self.work.write_text(template + '\n' + text, encoding='utf-8')

    def test_send_on_mark_and_rearm_after_uncheck(self):
        with patch.object(inbox, 'dispatch_request', return_value={'session': 'ses_chief'}) as send:
            self.mark('Первый срок', checked=False)
            self.assertEqual(inbox.process_inbox(self.vault), [])
            self.mark('Первый срок')
            receipt, = inbox.process_inbox(self.vault)
            self.assertIn('ses_chief', receipt.read_text(encoding='utf-8'))
            self.mark('Неоконченная правка')
            self.assertEqual(inbox.process_inbox(self.vault), [])
            self.assertEqual(send.call_count, 1)
            self.mark('Другой срок', checked=False)
            inbox.process_inbox(self.vault)
            self.mark('Другой срок')
            new_receipt, = inbox.process_inbox(self.vault)
            self.assertNotEqual(new_receipt, receipt)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(pending_requests(self.vault)[0]['text'], 'Другой срок')

    def test_checked_request_waits_for_project_mode_and_is_delivered_once(self):
        from kit.chief_mode import set_mode
        self.mark('Передать после включения')
        cache = {}
        set_mode(self.vault.root, False)
        with patch.object(inbox, 'dispatch_request', return_value={'session': 'ses_chief'}) as send:
            self.assertEqual(inbox.process_inbox(self.vault, cache=cache), [])
            send.assert_not_called()
            set_mode(self.vault.root, True)
            self.assertEqual(len(inbox.process_inbox(self.vault, cache=cache)), 1)
            self.assertEqual(inbox.process_inbox(self.vault, cache=cache), [])
            send.assert_called_once()

    def test_failed_delivery_has_durable_receipt_with_retry(self):
        with patch.object(inbox, 'dispatch_request', side_effect=RuntimeError('Служба недоступна')) as send:
            self.mark('Проверить критерий')
            path, = inbox.process_inbox(self.vault)
            request, = pending_requests(self.vault)
            receipt = path.read_text(encoding='utf-8')
            self.assertIn('Служба недоступна', receipt)
            self.assertIn('send --request ' + request['id'], receipt)
            self.assertEqual(inbox.process_inbox(ProjectVault.load(self.vault.root)), [])
            send.assert_called_once()

    def test_interruption_after_claim_records_unknown_outcome_without_redelivery(self):
        self.mark('Изменить состав')
        with patch.object(inbox, '_deliver', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                inbox.process_inbox(self.vault)
        with patch.object(inbox, 'dispatch_request') as send:
            path, = inbox.process_inbox(self.vault)
            self.assertIn('Передача прервана', path.read_text(encoding='utf-8'))
            self.assertEqual(inbox.process_inbox(self.vault), [])
            send.assert_not_called()

    def test_cli_retry_preserves_error_and_publishes_one_confirmation(self):
        self.mark('Вернуть возможность оплаты')
        failure = RuntimeError('Нет службы')
        with patch.object(inbox, 'dispatch_request', side_effect=[failure, failure, {'session': 'ses_restored'}]) as send:
            failed_note, = inbox.process_inbox(self.vault)
            request, = pending_requests(self.vault)
            with self.assertRaisesRegex(RuntimeError, 'Нет службы'):
                inbox.retry_delivery(self.vault, request)
            with patch.object(inbox, '_publish_outcomes', side_effect=OSError('Сбой публикации')):
                with self.assertRaisesRegex(OSError, 'Сбой публикации'):
                    inbox.retry_delivery(self.vault, request)
            with self.vault.locked() as db:
                row = db.execute("SELECT * FROM inbox_receipts WHERE status='Передано Чифу'").fetchone()
            occupied = inbox._receipt_path(self.vault, row)
            occupied.write_text('Личные пометки', encoding='utf-8')
            delivery = inbox.retry_delivery(self.vault, request)
            self.assertEqual(delivery, {'session': 'ses_restored'})
            self.assertEqual(inbox.retry_delivery(self.vault, request), delivery)
            self.assertEqual(send.call_count, 3)
            self.assertIn('Нет службы', failed_note.read_text(encoding='utf-8'))
            self.assertEqual(occupied.read_text(encoding='utf-8'), 'Личные пометки')
            confirmations = [p for p in occupied.parent.glob('*.md') if 'ses_restored' in p.read_text(encoding='utf-8')]
            self.assertEqual(len(confirmations), 1)

    def test_success_from_watcher_is_reused_by_cli_send(self):
        self.mark('Повторный запуск')
        with patch.object(inbox, 'dispatch_request', return_value={'session': 'ses_chief'}) as send:
            receipt, = inbox.process_inbox(self.vault)
            request, = pending_requests(self.vault)
            self.assertEqual(inbox.retry_delivery(self.vault, request), {'session': 'ses_chief'})
            self.assertEqual(list(receipt.parent.glob('*.md')), [receipt])
            send.assert_called_once()

    def test_unreadable_workspace_and_unwritable_receipt_do_not_block_other_records(self):
        self.mark('Операция A')
        self.vault.add('stage', 'broken', 'Путь занят каталогом')
        broken = self.vault.workspace('broken')
        broken.unlink()
        broken.mkdir()
        self.vault.add('stage', 'other', 'Другая работа')
        work = self.vault.workspace('other')
        work.write_text(work.read_text(encoding='utf-8').replace('- [ ] Передать Чифу', '- [x] Передать Чифу')
                        + '\nОперация Б', encoding='utf-8')
        directory = self.vault.path / 'Квитанции'
        directory.mkdir()
        (directory / 'plan').write_text('Этот файл принадлежит пользователю', encoding='utf-8')
        with patch.object(inbox, 'dispatch_request', return_value={'session': 'ses_chief'}) as send:
            paths = inbox.process_inbox(self.vault)
            self.assertEqual([p.parent.name for p in paths], ['other'])
            self.assertEqual(send.call_count, 2)

    def test_already_accepted_request_and_idle_note_do_not_dispatch(self):
        self.mark('Готово и проверено')
        request = request_revision(self.vault, 'plan')
        accept_revision(self.vault, 'plan', request['id'], 'Согласовано', 'Сверена запись')
        cache = {}
        with patch.object(inbox, 'dispatch_request') as send:
            receipt, = inbox.process_inbox(self.vault, cache=cache)
            self.assertIn('уже принято', receipt.read_text(encoding='utf-8'))
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('лишнее чтение')), \
                 patch.object(ProjectVault, 'locked', side_effect=AssertionError('лишняя блокировка')):
                self.assertEqual(inbox.process_inbox(self.vault, cache=cache), [])
            send.assert_not_called()

    def test_reopening_during_stop_starts_another_watcher(self):
        service = inbox.ensure_inbox_watcher(self.vault)
        try:
            # Запрошена остановка из другого запуска комплекта, который не владеет Popen.
            child = inbox._children.pop(service['pid'])
            try:
                inbox.stop_inbox_watcher(self.vault)
                new_service = inbox.ensure_inbox_watcher(self.vault)
                self.assertNotEqual(new_service['instance'], service['instance'])
            finally:
                child.wait(timeout=6)
        finally:
            inbox.stop_inbox_watcher(self.vault)

    def test_refresh_running_watcher_after_delivery_protocol_upgrade(self):
        self.assertEqual(inbox.ensure_inbox_watcher(self.vault, only_if_running=True), {})
        old = inbox.ensure_inbox_watcher(self.vault)
        child = inbox._children[old['pid']]
        try:
            legacy = dict(old)
            del legacy['watcher_version']
            inbox._write_state(self.vault, legacy)
            current = inbox.ensure_inbox_watcher(self.vault, only_if_running=True)
            self.assertNotEqual(current['instance'], old['instance'])
            self.assertEqual(current['watcher_version'], inbox.WATCHER_VERSION)
            self.assertEqual(child.wait(timeout=6), 0)
        finally:
            inbox.stop_inbox_watcher(self.vault)

    def test_one_background_process_consumes_real_workspace_and_stops(self):
        service = inbox.ensure_inbox_watcher(self.vault)
        try:
            self.assertEqual(inbox.ensure_inbox_watcher(self.vault), service)
            # Пустое описание проходит до настоящего валидатора, но не обращается к модели.
            self.mark('')
            deadline = time.monotonic() + 8
            folder = self.vault.path / 'Квитанции/plan'
            while not list(folder.glob('*.md')) and time.monotonic() < deadline:
                time.sleep(.1)
            path, = folder.glob('*.md')
            self.assertIn('Опишите изменение', path.read_text(encoding='utf-8'))
        finally:
            inbox.stop_inbox_watcher(self.vault)
        self.assertFalse(inbox._alive(service))
