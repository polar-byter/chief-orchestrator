"""Проверка сохранённых оснований, позднего табеля и повторов записи ресурсов этапа."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from kit.project_vault import ProjectVault
from kit.stage_resource_ledger import observe_forecast, record_resource, resource_snapshot, resource_view, tracked_stages
from kit.stage_resources import calculate_resources


class StageResourceLedgerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'проект'
        root.mkdir()
        self.vault = ProjectVault.connect(root)
        self.stage = 'delivery'
        self.vault.add('stage', self.stage, 'Поставка')
        self.forecast = self.estimate(12)
        self.start = (date.today() - timedelta(days=5)).isoformat()
        self.second = (date.today() - timedelta(days=4)).isoformat()
        self.third = (date.today() - timedelta(days=3)).isoformat()
        self.plan = dict(source='Календарь и договор', start=self.start, target=self.third,
                         weekdays=list(range(1, 8)), currency='RUB', extra_cost=0,
                         teams=[dict(role='Анализ', people=1, hours_per_day=4, allocation=1, focus=1, rate=100)])

    @staticmethod
    def estimate(hours):
        return dict(fingerprint=f'часы-{hours}', total_hours=hours, known_hours=hours, complete_count=1,
                    feature_count=1, resource_work=[dict(key='f/W1', role='Анализ', hours=str(hours),
                                                         o=str(hours), m=str(hours), p=str(hours), basis='Аналог')])

    def command(self, action, version=0, day=None, **kwargs):
        return dict(stage=self.stage, project_id=self.vault.project_id, action=action, author='РП', role='manager',
                    expected_version=version, request_id=str(uuid4()), date=day or self.start,
                    fingerprint=self.forecast['fingerprint']) | kwargs

    def actual(self, day, hours=None, cost=None, currency='RUB'):
        return dict(date=day, source='Табель', role='Анализ', hours=hours, cost=cost, currency=currency)

    def send(self, cmd):
        return record_resource(self.vault, self.stage, self.forecast, cmd)

    def view(self):
        return resource_view(self.vault, self.stage, self.forecast)

    def test_old_vault_opens_read_only_and_snapshots_stay_scoped(self):
        result = self.view()
        self.assertEqual((result['version'], result['history'], result['baseline']), (0, [], None))
        self.assertIsNone(result['current']['actual_hours'])
        self.assertEqual(tracked_stages(self.vault), [])
        self.assertIsNone(observe_forecast(self.vault, self.stage, lambda *args: self.fail('пустой журнал')))
        with self.vault.reading() as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='stage_resource_events'").fetchone())
        self.send(self.command('plan', plan={'source': 'Календарь запрошен'}))
        self.assertIsNone(self.view()['current']['finish'])
        self.vault.add('stage', 'other', 'Другой этап')
        self.assertEqual(tracked_stages(self.vault), [self.stage])
        with self.assertRaises(FileNotFoundError):
            resource_snapshot(self.vault, 'other', 1)
        with self.assertRaises(ValueError):
            self.send(self.command('actual', stage='other', actual=self.actual(self.start, 2)))

    def test_late_actual_rebuilds_effective_history_and_preserves_original_receipt(self):
        self.send(self.command('plan', plan=self.plan))
        third = self.send(self.command('actual', 1, actual=self.actual(self.third, 4, 400)))
        self.assertEqual(third['metrics']['actual_hours'], 4)
        self.send(self.command('actual', 2, actual=self.actual(self.second, 3, 240)))
        view = self.view()
        history = view['history']
        self.assertEqual([row['version'] for row in history], [1, 3, 2])
        self.assertEqual([row['metrics']['actual_hours'] for row in history], [None, 3, 7])
        self.assertEqual([row['metrics']['planned_hours_to_date'] for row in history], [4, 8, 12])
        self.assertEqual(view['current']['actual_cost'], 640)
        self.assertEqual(history[-1]['baseline']['version'], 1)
        saved = resource_snapshot(self.vault, self.stage, 2)
        self.assertEqual(saved['receipt'], third)
        self.assertEqual(saved['actuals'], [self.actual(self.third, 4, 400)])
        restored = calculate_resources(saved['forecast'], saved['plan'], saved['actuals'], saved['receipt']['date'])
        self.assertEqual(restored['actual_cost'], 400)

    def test_replan_changes_forecast_without_moving_earlier_baseline(self):
        self.send(self.command('plan', plan=self.plan))
        self.forecast = self.estimate(18)
        self.send(self.command('checkpoint', 1, self.second, source='Изменение объёма'))
        values = self.view()
        self.assertEqual((values['baseline']['total_hours'], values['current']['total_hours']), (12, 18))
        second = values['history'][-1]
        self.assertEqual((second['metrics']['total_hours'], second['baseline']['total_hours']), (18, 12))
        checkpoint = resource_snapshot(self.vault, self.stage, 2)
        self.assertEqual((checkpoint['forecast']['total_hours'], checkpoint['baseline']['forecast']['total_hours']), (18, 12))
        self.send(self.command('plan', 2, self.third, plan=self.plan | {'extra_cost': 50, 'source': 'Новая ставка'}))
        self.assertEqual(self.view()['baseline']['cost'], 1850)
        self.assertEqual(resource_snapshot(self.vault, self.stage, 1)['forecast']['total_hours'], 12)

    def test_concurrent_retries_and_stale_documents(self):
        payload = self.command('plan', plan=self.plan)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(self.send, [payload, payload]))
        self.assertEqual(receipts[0], receipts[1])
        changed = self.estimate(16)
        def fresh(vault, db, stage):
            self.assertTrue(db.in_transaction)
            return changed
        another = self.command('checkpoint', 1, source='Сверка')
        with self.assertRaisesRegex(ValueError, 'оценка'):
            record_resource(self.vault, self.stage, fresh, another)
        with self.assertRaisesRegex(ValueError, 'вер|Журнал'):
            self.send(self.command('actual', actual=self.actual(self.start, 1)))
        with self.assertRaisesRegex(ValueError, 'ID операции'):
            self.send(payload | {'author':'Другой'})
        self.assertEqual(self.view()['version'], 1)

    def test_bad_inputs_do_not_create_history_and_zero_is_evidence(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        invalid = [
            self.command('plan', plan=self.plan, role='engineer'),
            self.command('plan', plan=self.plan | {'dependencies':[dict(work='missing', after='f/W1', wait_days=0)]}),
            self.command('plan', plan=self.plan | {'api':[dict(model='m', input_tokens=-1)]}),
            self.command('actual', actual=self.actual(future, 1)),
            self.command('actual', actual=self.actual(self.start)),
            self.command('actual', actual=self.actual(self.start, cost=1, currency='')),
            self.command('actual', actual=self.actual(self.start, -1)),
            self.command('checkpoint', source=''),
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.send(payload)
        self.assertEqual(self.view()['version'], 0)
        self.send(self.command('actual', role='engineer', actual=self.actual(self.start, 0, 0)))
        self.assertEqual(self.view()['current']['actual_hours'], 0)
        self.assertEqual(self.view()['current']['actual_cost'], 0)

    def test_observer_versions_real_resource_changes_once(self):
        self.send(self.command('plan', plan=self.plan))
        notes_only = self.forecast | {'fingerprint':'обновлены BDD'}
        self.assertIsNone(observe_forecast(self.vault, self.stage, lambda *a: notes_only))
        changed = self.estimate(16)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: observe_forecast(self.vault, self.stage, lambda *a: changed), range(2)))
        saved, = [result for result in results if result]
        self.assertEqual((saved['action'], saved['author'], saved['metrics']['total_hours']), ('checkpoint', 'Чиф', 16))
        self.assertEqual(self.view()['baseline']['total_hours'], 12)
        self.assertEqual(self.view()['version'], 2)
        self.assertIsNone(observe_forecast(self.vault, self.stage, lambda *a: changed))
        restored = observe_forecast(self.vault, self.stage, lambda *a: self.forecast)
        self.assertEqual((restored['version'], restored['metrics']['total_hours']), (3, 12))

    def test_actual_between_edit_and_poll_preserves_the_forecast_change(self):
        self.send(self.command('plan', plan=self.plan))
        self.forecast = self.estimate(16)
        self.send(self.command('actual', version=1, actual=self.actual(self.start, 1, 100)))
        item = observe_forecast(self.vault, self.stage, lambda *_: self.forecast)
        self.assertEqual(item['metrics']['total_hours'], 16)
        first, late, observed = self.view()['history']
        self.assertEqual([p['metrics']['total_hours'] for p in (first, late, observed)], [12, 12, 16])
