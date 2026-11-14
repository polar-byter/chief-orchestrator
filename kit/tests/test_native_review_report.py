"""Финальная сводка показывает проверенные попытки по сохранённым при завершении данным."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit.dashboard.runtime import task_view
from kit.native_team_state import NativeTeamState
from kit.task_metrics import TaskMetrics
from kit.task_report import read_completed_task_report


class NativeReviewReportTests(unittest.TestCase):
    def test_provider_incident_without_candidate_survives_finish_and_does_not_count_as_rework(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = NativeTeamState(root, 'quota')
            state.initialize('claude')
            metrics = TaskMetrics(root)
            with metrics.lock('quota'):
                metrics.append('quota', 'start', native_agent='claude', session=None, title='Quota')
            job_id = str(uuid4())
            work = state.directory / 'workspaces' / job_id
            work.mkdir(parents=True)
            (work / 'price.py').write_bytes(b'original')
            state.enqueue(role='executor', assigned_file='price.py', workspace=work,
                          instructions='Уточнить цену', operation_id=job_id)
            job = state.claim()
            with state.admit(job_id, job['boundary']):
                pass
            stopped = state.begin_provider_stop(job_id, job['boundary'], 'limit', model='local',
                                                partial=b'PRIVATE DRAFT')
            state.confirm_stop(stopped['generation'], stopped['active'])
            with self.assertRaises(ValueError):
                metrics.finish('quota', language='en')
            state.resolve(job_id, 'discard')
            report = metrics.finish('quota', language='en')
            self.assertIn('Provider interruptions', report)
            self.assertIn('Provider limit', report)
            self.assertIn('13 B', report)
            self.assertNotIn('PRIVATE DRAFT', report)
            self.assertIn('Rework requests: **0**', report)
            self.assertEqual(report, metrics.finish('quota', language='en'))

    def test_finished_report_uses_both_attempts_and_excludes_private_fields(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = TaskMetrics(root)
            state = NativeTeamState(root, 'invoice')
            state.initialize('claude')
            with metrics.lock('invoice'):
                metrics.append('invoice', 'start', native_agent='claude', session=None, title='Invoice')
            check = dict(tests=1, failures=1, errors=0, skipped=0, test_name='test_invoice.test_rounding',
                         diagnostic='Assertion failed', stderr='PRIVATE-LOG')
            review = dict(number=1, root_id='first', previous_job=None, status='failed', remaining_total=2,
                          remaining_fixer=1, usage_scope='cli_result', usage=dict(input_tokens=0),
                          check=check, policy={'secret':'PRIVATE-KEY'})
            fixer = dict(review, number=2, previous_job='first', status='passed', usage={'output_tokens':35},
                         check=dict(check, failures=0, diagnostic=''))
            jobs = [dict(id='first', assigned_file='invoice.py', role='executor', review=review),
                    dict(id='second', assigned_file='invoice.py', role='bug_fixer', review=fixer)]
            view = dict(agent='claude', jobs=jobs)
            with patch.object(NativeTeamState, 'final_review_view', return_value=view, create=True) as read:
                report = metrics.finish('invoice', language='ru')
                for expected in ('Проверки и исправления', 'invoice.py', '| 1 | Исполнитель', '| 2 | Исправитель',
                                 'Проверка выявила дефект', 'Проверка пройдена', 'test_invoice.test_rounding',
                                 'Assertion failed', '2 / 1', '0 / нет данных', 'нет данных / 35'):
                    self.assertIn(expected, report)
                self.assertNotIn('PRIVATE', report)
                self.assertNotIn('PRIVATE', str(metrics.read('invoice')[-1]['usage']))
                jobs.clear()
                self.assertEqual(report, metrics.finish('invoice', language='ru'))
                english = read_completed_task_report(root, 'invoice', 'en')
                self.assertIn('| 2 | Fixer | Check passed', english)
                self.assertIn('Check failed', english)
                read.assert_called_once()
            self.assertEqual(task_view(root, 'invoice')['status'], 'finished')
