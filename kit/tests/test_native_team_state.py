"""Очередь, свидетельства остановки и восстановление работы нативной команды."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit import native_team_state
from kit.native_team_state import NativeTeamState


class NativeTeamStateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.team = NativeTeamState(self.root, 'change')
        self.team.initialize('codex')

    def worker(self, name='price.py', **kwargs):
        work = self.team.directory / 'workspaces' / str(uuid4())
        work.mkdir(parents=True)
        (work / name).write_bytes(b'base\r\n')
        return self.team.enqueue(role='executor', instructions='Исправить округление.', workspace=work,
                                 assigned_file=name, **kwargs)

    def chief(self, **kwargs):
        return self.team.enqueue(role='chief', instructions='Сообщить результат.', workspace=self.root, **kwargs)

    def start_one(self):
        job = self.team.claim()
        with self.team.admit(job['id'], job['boundary']):
            self.assertTrue(self.team.is_running(job['id'], job['boundary']))
        return job

    def pause(self, reason='pause'):
        stopping = self.team.begin_stop(reason)
        return self.team.confirm_stop(stopping['generation'], stopping['active'])

    def test_parallel_roles_and_fifo_for_one_logical_file_across_copies(self):
        chief1, chief2 = self.chief(), self.chief()
        first, second, other = self.worker(), self.worker(), self.worker('quantity.py')
        with ThreadPoolExecutor(max_workers=5) as pool:
            candidates = list(pool.map(lambda _: self.team.claim(), range(5)))
        actual = {job['id'] for job in candidates if job}
        self.assertEqual(actual, {chief1['id'], first['id'], other['id']})
        self.assertEqual(len(self.team.view()['active']), 3)
        before = self.team.job(chief1['id'])
        with self.team.admit(before['id'], before['boundary']):
            pass
        self.team.checkpoint(before['id'], before['boundary'], candidate=None, result={'text': 'Готово'})
        self.team.commit(before['id'], before['boundary'])
        self.assertEqual(chief2['id'], self.team.claim()['id'])
        self.assertEqual('queued', self.team.job(second['id'])['status'])

    def test_repeated_submission_matches_contents_and_hides_prompt_from_views(self):
        operation = str(uuid4())
        initial = self.chief(operation_id=operation)
        self.assertEqual(self.chief(operation_id=operation)['id'], initial['id'])
        with self.assertRaisesRegex(ValueError, 'UUID'):
            self.team.enqueue(role='chief', workspace=self.root, instructions='Другой вопрос.', operation_id=operation)
        self.assertNotIn('instructions', self.team.view()['jobs'][0])
        self.assertEqual(self.team.claim()['instructions'], initial['instructions'])
        with self.assertRaisesRegex(ValueError, 'среды'):
            self.team.initialize('claude')

    def test_assigned_file_must_be_in_the_trusted_task_workspace(self):
        (self.root / 'original.py').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.team.enqueue(role='executor', workspace=self.root, assigned_file='original.py', instructions='Пример')
        job = self.worker()
        for source in ('../original.py', 'price.py:alternate', 'NUL', 'price.py.'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.team.enqueue(role='executor', workspace=job['workspace'], assigned_file=source, instructions='Пример')
        with self.assertRaisesRegex(ValueError, 'контрактом'):
            self.team.enqueue(role='executor', workspace=job['workspace'], assigned_file='price.py',
                              contracts=['price.py'], instructions='Пример')
        self.assertEqual((self.root / 'original.py').read_bytes(), b'keep')

    def test_pause_closes_admission_before_os_confirmation_and_fences_stale_worker(self):
        self.worker()
        job = self.team.claim()
        queued = self.chief()
        stop = self.team.begin_stop()
        self.assertEqual(stop['status'], 'stopping')
        self.assertIsNone(self.team.claim())
        self.assertFalse(self.team.is_running(job['id'], job['boundary']))
        with self.assertRaisesRegex(ValueError, 'отозвано'), self.team.admit(job['id'], job['boundary']):
            self.fail('Старая попытка не должна дойти до запуска процесса')
        with self.assertRaisesRegex(ValueError, 'каждой'):
            self.team.confirm_stop(stop['generation'], [])
        with self.assertRaisesRegex(ValueError, 'устаревшее'):
            self.team.confirm_stop(stop['generation'] - 1, stop['active'])
        with self.assertRaisesRegex(ValueError, 'закрыт'):
            self.chief()
        paused = self.team.confirm_stop(stop['generation'], stop['active'])
        self.assertEqual(paused['status'], 'paused')
        with self.assertRaisesRegex(ValueError, 'решения'):
            self.team.resume()
        self.assertEqual(self.team.job(queued['id'])['status'], 'queued')
        self.team.resolve(job['id'], 'retry')
        self.team.resume()
        new = self.team.claim()
        self.assertEqual(new['id'], job['id'])
        self.assertNotEqual(new['boundary'], job['boundary'])
        with self.assertRaises(ValueError):
            self.team.checkpoint(job['id'], job['boundary'], candidate=b'old',
                                 result={})

    def test_admission_blocks_confirmation_until_os_child_has_been_registered(self):
        self.worker()
        job = self.team.claim()
        inside, started_stop, done = Event(), Event(), Event()

        def register_child():
            with self.team.admit(job['id'], job['boundary']):
                inside.set()
                self.assertTrue(started_stop.wait(5))
                self.assertFalse(done.wait(.2))

        def request_stop():
            self.assertTrue(inside.wait(5))
            started_stop.set()
            stopping = self.team.begin_stop()
            self.team.confirm_stop(stopping['generation'], stopping['active'])
            done.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(register_child), pool.submit(request_stop)
            a.result(10)
            b.result(10)
        self.assertEqual(self.team.view()['status'], 'paused')

    def test_limit_and_cancel_drop_the_queue_ordinary_pause_keeps_it(self):
        self.worker()
        active = self.start_one()
        queued = self.chief()
        first = self.team.begin_stop('pause')
        stronger = self.team.begin_stop('limit')
        self.assertGreater(stronger['generation'], first['generation'])
        self.assertEqual(self.team.job(queued['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.team.confirm_stop(first['generation'], first['active'])
        self.team.confirm_stop(stronger['generation'], stronger['active'])
        self.assertEqual(self.team.job(active['id'])['status'], 'interrupted')
        self.assertEqual(self.pause('cancel')['status'], 'cancelled')
        self.assertEqual(self.team.job(active['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.team.resume()

    def test_publication_survives_lost_response_without_another_write(self):
        self.worker()
        row = self.start_one()
        args = row['id'], row['boundary']
        target = Path(row['workspace']) / row['assigned_file']
        neighbor = target.with_name('contract.md')
        neighbor.write_bytes(b'only read')
        prepared = self.team.checkpoint(*args, candidate=b'new\r\n', result={'text': 'done'})
        self.assertEqual(prepared['candidate_hash'], sha256(b'new\r\n').hexdigest())
        self.assertEqual(target.read_bytes(), b'base\r\n')
        replace = native_team_state.replace_source
        def crash_after_replace(*parameters):
            replace(*parameters)
            raise OSError('Потеря процесса после замены')
        with patch.object(native_team_state, 'replace_source', side_effect=crash_after_replace), self.assertRaises(OSError):
            self.team.commit(*args)
        reopened = NativeTeamState(self.root, self.team.task)
        self.assertEqual(reopened.job(row['id'])['status'], 'prepared')
        stamp = target.stat().st_mtime_ns
        with patch.object(native_team_state, 'replace_source', side_effect=AssertionError('повтор записи')):
            stop = reopened.begin_stop('recovery')
            self.assertEqual(reopened.confirm_stop(stop['generation'], stop['active'])['jobs'][0]['status'], 'completed')
            self.assertEqual(reopened.commit(*args)['status'], 'completed')
        self.assertEqual(target.read_bytes(), b'new\r\n')
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertEqual(neighbor.read_bytes(), b'only read')

    def test_paused_checkpoint_requires_explicit_publication_after_file_check(self):
        self.worker()
        row = self.start_one()
        args = row['id'], row['boundary']
        target = Path(row['workspace']) / row['assigned_file']
        self.team.checkpoint(*args, candidate=b'', result={'text': 'Пустой файл по условию.'})
        self.pause()
        with self.assertRaisesRegex(ValueError, 'приостановлена'):
            self.team.commit(*args)
        target.write_bytes(b'owner edit')
        with self.assertRaisesRegex(ValueError, 'изменился'):
            self.team.resolve(row['id'], 'publish')
        with self.assertRaisesRegex(ValueError, 'изменился'):
            self.team.resolve(row['id'], 'retry')
        self.assertEqual(target.read_bytes(), b'owner edit')
        target.write_bytes(b'base\r\n')
        self.team.resolve(row['id'], 'publish')
        self.assertEqual(target.read_bytes(), b'')
        self.assertEqual(self.team.resume()['active'], [])

    def test_queue_edit_and_failure_have_no_implicit_retry(self):
        first, second = self.worker(), self.chief()
        self.assertEqual(self.team.cancel_queued(first['id'])['status'], 'cancelled')
        job = self.start_one()
        self.assertEqual(job['id'], second['id'])
        self.assertEqual(self.team.fail(job['id'], job['boundary'], 'Ошибка API')['status'], 'failed')
        self.assertIsNone(self.team.claim())
        self.worker()
        self.assertEqual(self.team.clear_queue(), 1)
        self.assertEqual(self.team.clear_queue(), 0)
