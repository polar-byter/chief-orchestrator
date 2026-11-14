"""Сверка входов, полномочий РП и однократной фиксации переоценки."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths
from kit.stage_collaboration import apply_action
from kit.stage_decisions import decide, decision_view


class StageDecisionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'проект'
        root.mkdir()
        self.vault = ProjectVault.connect(root)
        self.stage = 'first'
        self.vault.add('stage', self.stage, 'Документация')

    def feature(self, key, value=None, reserve=None):
        self.vault.add('feature', key, key, self.stage)
        if value is not None:
            self.estimate(key, value, reserve)

    def estimate(self, key, value, reserve=None):
        notes = section_paths(self.vault, key)
        notes['effort'].write_text('| Работа / ID | Роль | Единица | O | M | P | Источник и допущение |\n'
                                   f'| W1 | Инженер | чел·ч | {value} | {value} | {value} | Эксперт |\n', encoding='utf-8')
        risk = '| ID риска | ID работ | Учёт | Резерв, чел·ч |\n'
        if reserve is not None:
            risk += f'| R1 | W1 | отдельно | {reserve} |\n'
        notes['risks'].write_text(risk, encoding='utf-8')

    def command(self, action, **kwargs):
        return dict(project_id=self.vault.project_id, request_id=str(uuid4()), action=action,
                    role='manager', author='Руководитель', reason='Проверка условий этапа') | kwargs

    def send(self, action, **kwargs):
        return decide(self.vault, self.stage, self.command(action, **kwargs))

    def view(self):
        return decision_view(self.vault, self.stage)

    def propose(self, **kwargs):
        return self.send('propose', fingerprint=self.view()['forecast']['fingerprint'], **kwargs)

    def approve_baseline(self):
        item = self.propose()
        return self.send('accept', proposal=item['id'])

    def delegate(self, limit):
        return self.send('policy', mode='auto', max_delta_hours=limit, policy_version=0,
                         valid_until=(date.today() + timedelta(days=2)).isoformat())

    def test_partial_coverage_unknown_zero_and_invalid_sources(self):
        self.feature('one', 0)
        self.feature('two')
        forecast = self.view()['forecast']
        self.assertEqual((forecast['complete_count'], forecast['feature_count'], forecast['known_hours']), (1, 2, 0))
        self.assertIsNone(forecast['total_hours'])
        self.assertIsNone(forecast['features'][1]['known_hours'])
        proposal = self.propose()
        with self.assertRaisesRegex(ValueError, 'полные'):
            self.send('accept', proposal=proposal['id'])
        self.assertEqual(self.send('reject', proposal=proposal['id'])['status'], 'rejected')
        section_paths(self.vault, 'one')['risks'].unlink()
        forecast = self.view()['forecast']
        self.assertIsNone(forecast['known_hours'])
        self.assertTrue(forecast['issues'])

    def test_local_risks_and_one_explicit_shared_event(self):
        self.feature('one', 2, 3)
        self.feature('two', 4, 3)
        self.assertEqual(self.view()['forecast']['total_hours'], 12)
        note = section_paths(self.vault, self.stage)['risks']
        note.write_text('| Общее событие | Риски фич | Где учтено |\n'
                        '| S1 | one/R1, two/R1 | one/R1 |\n', encoding='utf-8')
        self.assertEqual(self.view()['forecast']['total_hours'], 9)
        forecast = self.view()['forecast']
        self.assertEqual([(row['key'], row['hours']) for row in forecast['resource_work']],
                         [('one/W1', '2'), ('one/@R1', '3'), ('two/W1', '4')])
        self.assertEqual(forecast['shared_reserves'], {'two/R1': 'one/R1'})
        self.assertEqual(self.approve_baseline()['total_hours'], 9)
        note.write_text(note.read_text(encoding='utf-8').replace('one/R1 |','three/R1 |'), encoding='utf-8')
        forecast = self.view()['forecast']
        self.assertIsNone(forecast['total_hours'])
        self.assertTrue(forecast['issues'])

    def test_stage_overhead_without_features_keeps_its_source_role_and_exact_pert(self):
        self.estimate(self.stage, 8)
        path = section_paths(self.vault, self.stage)['effort']
        path.write_text(path.read_text(encoding='utf-8').replace('8 | 8 | 8', '8 | 12 | 24'), encoding='utf-8')
        forecast = self.view()['forecast']
        self.assertEqual((forecast['total_hours'], forecast['feature_count']), (13.3, 0))
        row, = forecast['resource_work']
        self.assertEqual((row['hours'], row['role'], row['p']), ('40/3', 'Инженер', '24'))
        self.feature('one', 1)
        self.assertEqual(self.view()['forecast']['total_hours'], 14.3)

    def test_old_revision_and_current_approved_values_remain_after_edit(self):
        self.feature('one', 8)
        initial = self.approve_baseline()
        self.estimate('one', 11)
        item = self.propose()
        section_paths(self.vault, 'one')['bdd'].write_text('Другой критерий', encoding='utf-8')
        view = self.view()
        self.assertEqual(view['approved']['id'], initial['id'])
        self.assertIn('Документы/one/BDD.md', view['proposals'][0]['changed_inputs'])
        with self.assertRaisesRegex(ValueError, 'устарело'):
            self.send('accept', proposal=item['id'])
        self.assertEqual(self.propose(replaces=item['id'])['replaces'], item['id'])

    def test_auto_boundary_uses_original_baseline_and_stores_policy_author(self):
        self.feature('one', 8)
        self.approve_baseline()
        self.delegate(2)
        self.estimate('one', 10)
        automatic = self.propose()
        self.assertEqual((automatic['status'],automatic['method'],automatic['delta_hours']), ('accepted','auto',2))
        self.assertEqual(automatic['policy_author'], 'Руководитель')
        self.estimate('one', 12)
        beyond = self.propose()
        self.assertEqual(beyond['status'], 'pending')
        self.assertIn('предел', beyond['decision_reason'])
        self.assertEqual(self.view()['approved']['id'], automatic['id'])
        self.estimate('one', 9)
        path = section_paths(self.vault, 'one')['effort']
        path.write_text(path.read_text(encoding='utf-8').replace('W1','W2'), encoding='utf-8')
        self.assertIn('границы', self.propose()['decision_reason'])

    def test_blocking_question_closed_then_reopened(self):
        self.feature('one', 4)
        self.approve_baseline()
        self.delegate(2)
        self.estimate('one', 5)
        def question(action, **ctx):
            return apply_action(self.vault, self.stage, dict(project_id=self.vault.project_id, operation_id=str(uuid4()),
                role='analyst', author='Аналитик', assignee='engineer', action=action, text='Уточнить S1', **ctx))
        thread = question('ask', feature='one', revision=1, subject='Пересчёт S1', blocking=True)
        pending = self.propose()
        self.assertIn('Пересчёт S1', pending['decision_reason'])
        with self.assertRaisesRegex(ValueError, 'Блокирующий'):
            self.send('accept', proposal=pending['id'])
        question('reply', thread_id=thread['id'], expected_version=1)
        self.assertEqual(self.send('accept', proposal=pending['id'])['method'], 'manual')
        question('reopen', thread_id=thread['id'], expected_version=2)
        self.assertIn('Блокирующий', self.propose()['decision_reason'])

    def test_concurrent_replay_and_other_project(self):
        self.feature('one', 3)
        item = self.propose()
        payload = self.command('accept', proposal=item['id'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: decide(self.vault, self.stage, payload), range(2)))
        self.assertEqual(results[0], results[1])
        with self.vault.locked() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM stage_decision_events WHERE action='accept'").fetchone()[0], 1)
        with self.assertRaisesRegex(ValueError, 'ID операции'):
            decide(self.vault, self.stage, payload | {'reason':'Другое решение'})
        with self.assertRaisesRegex(ValueError, 'другому проекту'):
            decide(self.vault, self.stage, payload | {'project_id':str(uuid4())})
        with self.assertRaisesRegex(ValueError, 'РП'):
            self.send('policy', role='engineer', mode='manual', policy_version=0)

    def test_scope_change_missing_authority_and_zero_limit(self):
        self.feature('one', 1)
        with self.assertRaisesRegex(ValueError, 'исходную'):
            self.delegate(0)
        self.approve_baseline()
        self.delegate(0)
        with self.assertRaisesRegex(ValueError, 'Политика изменилась'):
            self.delegate(0)
        self.feature('two', 0)
        self.assertIn('границы', self.propose()['decision_reason'])
        self.assertIsNotNone(self.view()['approved'])
