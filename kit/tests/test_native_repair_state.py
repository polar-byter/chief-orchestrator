"""История приёмки, остатки бюджета и отмена допуска к проверке кандидата."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from kit.native_team_state import NativeTeamState


class NativeRepairStateTests(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.team = NativeTeamState(Path(area.name), 'price-review')
        self.team.initialize('codex')
        self.policy = dict(maxAttempts=4, fixerMaxAttempts=2)

    def assignment(self, content=b'original', *, parent=None, policy=None, role=None, **kwargs):
        identifier = str(uuid4())
        work = self.team.directory / 'workspaces' / identifier
        work.mkdir(parents=True)
        (work / 'price.py').write_bytes(content)
        (work / 'test_price.py').write_text('contract', encoding='utf-8')
        return dict(role=role or ('bug_fixer' if parent else 'executor'), instructions='Округлить цену.',
                    contracts=['test_price.py'], assigned_file='price.py', workspace=work, operation_id=identifier,
                    verification=self.policy if policy is None else policy, previous_job=parent, **kwargs)

    def complete(self, job, candidate):
        claimed = self.team.claim()
        self.assertEqual(claimed['id'], job['id'])
        key = job['id'], claimed['boundary']
        with self.team.admit(*key):
            pass
        self.team.checkpoint(*key, candidate=candidate,
                             result={'usage': {'input_tokens': 12, 'output_tokens': 0, 'opaque': 'private'},
                                     'usage_scope': 'cli_result'})
        self.team.commit(*key)

    def check(self, job, verdict, diagnostics=''):
        key = job['id'], str(uuid4())
        self.assertTrue(self.team.begin_check(*key))
        with self.team.check_admission(*key):
            self.assertTrue(self.team.check_running(*key))
        result = self.team.job(job['id'])
        evidence = dict(verdict=verdict, candidate_hash=result['candidate_hash'], tests=int(verdict != 'blocked'),
                        failures=int(verdict == 'failed'), errors=0, skipped=0, diagnostic=diagnostics,
                        test_name='test_price.test_rounding', test_hashes={'test_price.py': sha256(b'contract').hexdigest()})
        self.assertTrue(self.team.finish_check(*key, evidence))
        self.assertFalse(self.team.finish_check(*key, evidence))
        return self.team.review_info(job['id'])

    def test_transfers_rejected_bytes_remaining_budget_concurrent_idempotent_child_and_original_hash(self):
        job = self.team.enqueue(**self.assignment())
        self.complete(job, b'wrong')
        self.assertEqual(self.team.pending_checks(), [job['id']])
        with self.assertRaisesRegex(ValueError, 'проверка'), self.team.integration_job(job['id']):
            pass
        failed = self.check(job, 'failed', 'Проверка округления не прошла.')
        self.assertEqual((failed['used_executor'],failed['used_fixer'],failed['remaining_total'],failed['remaining_fixer']),
                         (1, 0, 3, 2))
        self.assertEqual(failed['usage'], {'input_tokens': 12, 'output_tokens': 0})
        self.assertEqual(self.team.pending_handoffs(), [job['id']])
        bad = self.assignment(b'wrong', parent=job['id'])
        bad['instructions'] = 'Изменённые требования'
        for changes in (bad, self.assignment(b'other', parent=job['id']),
                        self.assignment(b'wrong', parent=job['id'], policy={'maxAttempts':20})):
            with self.assertRaises(ValueError):
                self.team.enqueue(**changes)
        self.assertEqual(len(self.team.view()['jobs']), 1)
        child = self.assignment(b'wrong', parent=job['id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: self.team.enqueue(**child)['id'], range(2)))
        self.assertEqual(ids[0], ids[1])
        with self.assertRaisesRegex(ValueError, 'уже назначен'):
            self.team.enqueue(**self.assignment(b'wrong', parent=job['id']))
        self.assertEqual(self.team.pending_handoffs(), [])
        self.complete(self.team.job(ids[0]), b'fixed')
        passed = self.check(self.team.job(ids[0]), 'passed')
        self.assertEqual((passed['root_id'],passed['previous_job'],passed['number']), (job['id'],job['id'],2))
        self.assertEqual(passed['original_hash'],sha256(b'original').hexdigest())
        self.assertEqual(passed['remaining_total'], 3)
        with self.team.integration_job(ids[0]) as ready:
            self.assertEqual(ready['review']['original_hash'], passed['original_hash'])

        with self.assertRaisesRegex(ValueError, 'перенесите'):
            self.team.final_review_view()
        source = self.team.job(ids[0])
        receipt = self.team.directory / 'assignments' / (ids[0] + '.integrated.json')
        receipt.parent.mkdir()
        receipt.write_text(json.dumps(dict(job=ids[0], path='price.py', previous_hash=passed['original_hash'],
                                           hash=source['candidate_hash'])),encoding='utf-8')
        closed = self.team.final_review_view()
        self.assertEqual(closed['status'], 'finished')
        self.assertEqual(self.team.final_review_view(), closed)
        self.assertEqual(self.team.begin_stop()['status'], 'finished')
        with self.assertRaisesRegex(ValueError, 'зафиксирован'), self.team.integration_job(ids[0]):
            pass
        with self.assertRaisesRegex(ValueError, 'закрыт'):
            self.team.enqueue(**self.assignment())

    def test_infrastructure_error_does_not_charge_or_replay_check_after_reload(self):
        job = self.team.enqueue(**self.assignment())
        self.complete(job, b'wrong')
        blocked = self.check(job, 'blocked', 'Не запустилась среда теста.')
        self.assertEqual((blocked['remaining_total'], blocked['used_fixer']), (4,0))
        self.team = NativeTeamState(self.team.root, self.team.task)
        self.assertEqual(self.team.pending_checks(), [])
        self.assertEqual(self.team.pending_handoffs(), [])
        self.team.recheck(job['id'])
        repaired = self.check(job, 'failed')
        self.assertEqual(repaired['remaining_total'],3)
        with self.assertRaises(ValueError):
            self.team.recheck(job['id'])
        with closing(sqlite3.connect(self.team.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_checks WHERE finished_at IS NOT NULL").fetchone()[0], 2)

    def test_exhaustion_counts_rejected_code_once_per_role_and_per_chain(self):
        for policy in (dict(maxAttempts=2,fixerMaxAttempts=2), dict(maxAttempts=4,fixerMaxAttempts=1)):
            with self.subTest(policy=policy):
                executor = self.team.enqueue(**self.assignment(policy=policy))
                self.complete(executor, b'wrong')
                self.check(executor, 'failed')
                fixer = self.team.enqueue(**self.assignment(b'wrong',parent=executor['id'],policy=policy))
                self.complete(fixer, b'wrong again')
                failed = self.check(fixer, 'failed')
                self.assertEqual((failed['used_executor'], failed['used_fixer']), (1,1))
                self.assertNotIn(fixer['id'], self.team.pending_handoffs())
                with self.assertRaisesRegex(ValueError,'исчерпан'):
                    self.team.enqueue(**self.assignment(b'wrong again', parent=fixer['id'], policy=policy))
                with self.assertRaisesRegex(ValueError,'успешно'):
                    self.team.final_review_view()

    def test_pause_preserves_pending_handoff_and_clear_or_quota_closes_it(self):
        for reason in ('pause', 'clear', 'limit'):
            with self.subTest(reason=reason):
                job = self.team.enqueue(**self.assignment())
                self.complete(job, b'bad')
                self.check(job, 'failed')
                if reason == 'clear':
                    self.team.clear_queue()
                else:
                    stopped = self.team.begin_stop(reason)
                    self.team.confirm_stop(stopped['generation'], stopped['active'])
                    self.team.resume()
                self.assertEqual(job['id'] in self.team.pending_handoffs(), reason == 'pause')
                if reason != 'pause':
                    with self.assertRaises(ValueError):
                        self.team.enqueue(**self.assignment(b'bad', parent=job['id']))
                self.team.clear_queue()

    def test_prevents_concurrent_checks_and_requires_confirmation_and_explicit_check_only_retry(self):
        job = self.team.enqueue(**self.assignment())
        self.complete(job, b'candidate')
        with ThreadPoolExecutor(max_workers=2) as pool:
            boundaries = [str(uuid4()), str(uuid4())]
            claims = list(pool.map(lambda b:self.team.begin_check(job['id'], b, 1), boundaries))
        self.assertEqual(sum(claims), 1)
        boundary = boundaries[claims.index(True)]
        with self.team.check_admission(job['id'], boundary):
            pass
        stopping = self.team.begin_stop('recovery')
        self.assertEqual(stopping['active'], [boundary])
        self.assertFalse(self.team.check_running(job['id'], boundary))
        with self.assertRaisesRegex(ValueError,'каждой'):
            self.team.confirm_stop(stopping['generation'], [])
        self.team.confirm_stop(stopping['generation'], [boundary])
        self.assertEqual(self.team.job(job['id'])['review']['status'], 'interrupted')
        self.assertFalse(self.team.finish_check(job['id'], boundary, {}))
        with self.assertRaises(ValueError):
            self.team.resume()
        with self.assertRaisesRegex(ValueError,'прерван'):
            self.team.resolve(job['id'], 'publish')
        self.team.resolve(job['id'], 'retry')
        self.team.resume()
        self.assertIsNone(self.team.claim())
        self.assertEqual(self.team.pending_checks(), [job['id']])
        self.assertEqual(self.team.job(job['id'])['attempt'],1)
        self.assertFalse(self.team.begin_check(job['id'], str(uuid4()), 1))
        self.assertEqual(self.check(job, 'passed')['remaining_total'],4)

    def test_normal_attempts_and_old_databases_stay_readable_without_migration(self):
        self.team.enqueue(role='chief', instructions='Прочитай контракты.', workspace=self.team.root)
        with closing(sqlite3.connect(self.team.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='repair_attempts'").fetchone())
        self.assertNotIn('review', self.team.operator_view()['jobs'][0])
        self.assertEqual(self.team.final_review_view()['status'], 'running')
        self.team.view()
        with closing(sqlite3.connect(self.team.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='repair_attempts'").fetchone())

    def test_result_tied_to_exact_candidate_admission_and_working_copy(self):
        job = self.team.enqueue(**self.assignment())
        self.complete(job, b'bad')
        key = job['id'], str(uuid4())
        self.assertTrue(self.team.begin_check(*key))
        evidence = dict(verdict='failed', candidate_hash=sha256(b'bad').hexdigest(), tests=1, failures=1, errors=0, skipped=0)
        with self.assertRaisesRegex(ValueError,'допуск'):
            self.team.finish_check(*key, evidence)
        with self.team.check_admission(*key):
            pass
        with self.assertRaises(ValueError):
            self.team.finish_check(*key, dict(evidence,candidate_hash='0'*64))
        with self.assertRaises(ValueError):
            self.team.finish_check(*key, dict(evidence,failures=0,errors=1))
        (Path(job['workspace'])/'price.py').write_bytes(b'foreign')
        self.assertTrue(self.team.finish_check(*key, evidence))
        self.assertEqual(self.team.review_info(job['id'])['status'],'blocked')
        self.assertEqual(self.team.pending_handoffs(), [])

