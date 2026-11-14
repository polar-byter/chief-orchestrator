"""Остановка из-за провайдера, сохранность черновика и граница между попытками."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from kit.native_provider_state import MAX_PARTIAL_BYTES
from kit.native_team_state import NativeTeamState


class ProviderStopTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.team = NativeTeamState(Path(area.name), 'provider-check')
        self.team.initialize('codex')

    def enqueue(self, name):
        identifier = str(uuid4())
        work = self.team.directory / 'workspaces' / identifier
        work.mkdir(parents=True)
        (work / name).write_bytes(b'original')
        return self.team.enqueue(role='executor', instructions='Поправить расчёт.', assigned_file=name,
                                 workspace=work, operation_id=identifier,
                                 verification={'maxAttempts': 3, 'fixerMaxAttempts': 2})

    def admit_next(self):
        row = self.team.claim()
        with self.team.admit(row['id'], row['boundary']):
            pass
        return row['id'], row['boundary']

    def confirm(self, view):
        return self.team.confirm_stop(view['generation'], view['active'])

    def test_api_pause_and_retry_keep_original_budget_model_and_partial_across_restart(self):
        original = self.enqueue('price.py')
        job, boundary = self.admit_next()
        draft = 'полученная часть\r\n'.encode()
        args = dict(partial=draft, model='vendor/qwen:4b')
        stop = self.team.begin_provider_stop(job, boundary, 'api_error', **args)
        self.assertEqual(self.team.begin_provider_stop(job, boundary, 'api_error', **args), stop)
        self.assertEqual((stop['status'], stop['reason'], stop['active']), ('stopping', 'api_error', [boundary]))
        self.assertIsNone(self.team.claim())
        self.assertRaises(ValueError, self.team.confirm_stop, stop['generation'], [])
        self.confirm(stop)
        self.assertRaises(ValueError, self.team.resolve, job, 'publish')
        self.team.resolve(job, 'retry')
        self.team.resume()
        self.team = NativeTeamState(self.team.root, self.team.task)
        self.assertEqual(self.team.provider_partial(job, 1), draft)
        self.assertEqual((Path(original['workspace']) / 'price.py').read_bytes(), b'original')
        with self.assertRaises(ValueError):
            self.team.provider_partial(job, 2)

        expected = [dict(attempt=1, reason='api_error', model='vendor/qwen:4b', partial_size=len(draft),
                         partial_sha256=sha256(draft).hexdigest())]
        state = self.team.operator_view()
        self.assertNotIn('полученная часть', json.dumps(state, ensure_ascii=False))
        self.assertEqual(state['jobs'][0]['provider_incidents'], expected)
        row = self.team.job(job)
        self.assertEqual(row['provider_incidents'], expected)
        self.assertIsNone(row['candidate_hash'])
        self.assertEqual((row['review']['remaining_total'], row['review']['remaining_fixer']), (3, 2))
        self.assertEqual(self.team.pending_handoffs(), [])
        second_job, second_boundary = self.admit_next()
        self.assertEqual(second_job, job)
        self.assertNotEqual(boundary, second_boundary)
        self.assertIsNone(self.team.begin_provider_stop(job, boundary, 'limit'))
        self.assertTrue(self.team.is_running(job, second_boundary))

        stop = self.team.begin_provider_stop(job, second_boundary, 'limit', partial=b'')
        self.confirm(stop)
        self.team.resolve(job, 'discard')
        self.assertEqual(self.team.provider_partial(job, 2), b'')
        self.assertEqual(self.team.provider_partial(job, 1), draft)
        self.assertEqual([x['partial_size'] for x in self.team.view()['jobs'][0]['provider_incidents']], [len(draft), 0])

    def test_competing_api_and_quota_signals_preserve_both_receipts_and_close_queue(self):
        self.enqueue('price.py')
        self.enqueue('quantity.py')
        queued = self.enqueue('total.py')
        first = self.admit_next()
        second = self.admit_next()
        with ThreadPoolExecutor(max_workers=2) as pool:
            api = pool.submit(self.team.begin_provider_stop, *first, 'api_error')
            quota = pool.submit(self.team.begin_provider_stop, *second, 'limit')
            self.assertIsNotNone(api.result())
            self.assertIsNotNone(quota.result())
        stop = self.team.view()
        self.assertEqual(stop['reason'], 'limit')
        self.assertEqual(self.team.job(queued['id'])['status'], 'cancelled')
        self.assertEqual([row['provider_incidents'][0]['reason'] for row in stop['jobs'][:2]], ['api_error', 'limit'])
        paused = self.confirm(stop)
        stronger = self.team.begin_stop('cancel')
        self.assertGreater(stronger['generation'], paused['generation'])
        self.assertEqual(self.confirm(stronger)['status'], 'cancelled')
        self.assertIsNone(self.team.begin_provider_stop(*first, 'api_error'))

    def test_late_quota_during_confirmed_pause_preserves_checkpoint_and_ordinary_api_keeps_queue(self):
        self.enqueue('price.py')
        self.enqueue('quantity.py')
        self.enqueue('sum.py')
        queued = self.enqueue('fee.py')
        failure = self.admit_next()
        sibling = self.admit_next()
        late = self.admit_next()
        self.team.checkpoint(*sibling, candidate=b'complete', result={})
        stop = self.team.begin_provider_stop(*failure, 'api_error')
        self.assertEqual(self.team.job(queued['id'])['status'], 'queued')
        self.confirm(stop)
        quota = self.team.begin_provider_stop(*late, 'limit')
        self.assertEqual(self.confirm(quota)['reason'], 'limit')
        self.assertEqual(self.team.job(queued['id'])['status'], 'cancelled')
        self.assertRaises(ValueError, self.team.provider_partial, failure[0], 1)
        self.assertEqual(self.team.resolve(sibling[0], 'publish')['status'], 'completed')

    def test_invalid_partial_rolls_back_and_model_receipt_omits_private_values(self):
        self.enqueue('price.py')
        job = self.admit_next()
        for partial in (b'\0', b'\xff', b'x' * (MAX_PARTIAL_BYTES + 1)):
            with self.subTest(size=len(partial)), self.assertRaises(ValueError):
                self.team.begin_provider_stop(*job, 'limit', partial=partial)
            self.assertTrue(self.team.is_running(*job))
        stop = self.team.begin_provider_stop(*job, 'api_error', model='https://secret.example/with-key')
        record, = stop['jobs'][0]['provider_incidents']
        self.assertIsNone(record['partial_size'])
        self.assertIsNone(record['partial_sha256'])
        self.assertIsNone(record['model'])
        with self.assertRaisesRegex(ValueError, 'другое свидетельство'):
            self.team.begin_provider_stop(*job, 'api_error', partial=b'changed')

    def test_unreported_legacy_attempt_reads_without_schema_writes(self):
        job = self.enqueue('price.py')
        self.assertNotIn('provider_incidents', self.team.job(job['id']))
        self.assertNotIn('provider_incidents', self.team.operator_view()['jobs'][0])
        with self.assertRaises(ValueError):
            self.team.provider_partial(job['id'], 1)
        with closing(sqlite3.connect(self.team.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_incidents'").fetchone())

    def test_incident_only_team_cannot_finish_before_operator_resolves_and_closes_queue(self):
        self.team.enqueue(role='chief', instructions='Прочитать требования.', workspace=self.team.root)
        job = self.admit_next()
        queued = self.team.enqueue(role='chief', instructions='Сверить выводы.', workspace=self.team.root)
        self.confirm(self.team.begin_provider_stop(*job, 'api_error'))
        with self.assertRaisesRegex(ValueError, 'закройте'):
            self.team.final_review_view()
        self.team.resolve(job[0], 'discard')
        with self.assertRaisesRegex(ValueError, 'закройте'):
            self.team.final_review_view()
        self.team.cancel_queued(queued['id'])
        view = self.team.final_review_view()
        self.assertEqual(view['status'], 'finished')
        self.assertEqual(view['jobs'][0]['status'], 'cancelled')
        self.assertNotIn('review', view['jobs'][0])
        self.assertEqual(view['jobs'][0]['provider_incidents'][0]['reason'], 'api_error')

    def test_finish_preserves_green_receipt_alongside_unstarted_quota_cancellations(self):
        accepted = self.enqueue('price.py')
        job = self.admit_next()
        self.team.checkpoint(*job, candidate=b'accepted', result={})
        row = self.team.commit(*job)
        check = (job[0], str(uuid4()))
        self.team.begin_check(*check)
        with self.team.check_admission(*check):
            pass
        self.team.finish_check(*check, dict(candidate_hash=row['candidate_hash'], verdict='passed',
                                            tests=1, failures=0, errors=0, skipped=0))
        receipts = self.team.directory / 'assignments'
        receipts.mkdir()
        (receipts / (job[0] + '.integrated.json')).write_text(json.dumps(
            dict(job=job[0], path='price.py', previous_hash=accepted['baseline_hash'], hash=row['candidate_hash'])))
        self.enqueue('fee.py')
        self.enqueue('tax.py')
        active = self.admit_next()
        self.confirm(self.team.begin_provider_stop(*active, 'limit'))
        self.team.resolve(active[0], 'discard')
        closed = self.team.final_review_view()
        self.assertEqual(closed['status'], 'finished')
        self.assertEqual([j['review']['status'] for j in closed['jobs']], ['passed', 'discarded', 'discarded'])
        self.assertTrue(closed['jobs'][0]['integrated'])
        self.assertIsNone(closed['jobs'][1]['review']['check'])

    def test_unstarted_cancelled_fixer_keeps_red_parent_out_of_acceptance(self):
        job = self.enqueue('price.py')
        active = self.admit_next()
        self.team.checkpoint(*active, candidate=b'wrong', result={})
        row = self.team.commit(*active)
        check = (job['id'], str(uuid4()))
        self.team.begin_check(*check)
        with self.team.check_admission(*check):
            pass
        self.team.finish_check(*check, dict(candidate_hash=row['candidate_hash'], verdict='failed', tests=1,
                                            failures=1, errors=0, skipped=0))
        work = self.team.directory / 'workspaces' / str(uuid4())
        work.mkdir()
        (work / 'price.py').write_bytes(b'wrong')
        fixer = self.team.enqueue(role='bug_fixer', workspace=work, instructions=job['instructions'],
                                  assigned_file='price.py', previous_job=job['id'], verification=job['review']['policy'])
        self.team.cancel_queued(fixer['id'])
        with self.assertRaisesRegex(ValueError, 'успешно'):
            self.team.final_review_view()
