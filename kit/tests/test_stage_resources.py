"""Рабочий календарь, общие ресурсы и деньги в оценке одного этапа."""
import unittest

from kit.stage_resources import ActualTotals, calculate_resources, evaluate_plan_at


def forecast(*rows, total=16):
    return dict(total_hours=total, known_hours=total, complete_count=1, feature_count=1,
                resource_work=[dict(key=key, role=role, hours=hours) for key, role, hours in rows])


def team(role, rate='800'):
    return dict(role=role, people=1, hours_per_day=8, allocation='1', focus='1', rate=rate)


def plan():
    return dict(start='2026-11-13', target='2026-11-20', weekdays=[1, 2, 3, 4, 5],
                holidays=['2026-11-16'], currency='RUB', extra_cost=0,
                teams=[team('Инженер'), team('Аналитик', rate=400)], dependencies=[])


def fact(day, role='Инженер', hours=1, cost=None, currency=''):
    return dict(date=day, role=role, hours=hours, cost=cost, currency=currency, source='Табель')


class StageResourceTests(unittest.TestCase):
    def test_two_teams_work_in_parallel_and_one_team_shares_capacity(self):
        independent = forecast(('f/A', 'Аналитик', 8), ('f/B', 'Инженер', 8))
        view = calculate_resources(independent, plan(), [], as_of='2026-11-13')
        self.assertEqual(view['finish'], '2026-11-13')
        self.assertEqual(view['working_days'], 5)
        analyst, engineer = view['teams']
        self.assertEqual((engineer['daily_hours'], engineer['capacity_hours'], engineer['load_percent']), (8, 40, 20))
        self.assertEqual(analyst['demand_hours'], 8)
        self.assertEqual((view['cost'], view['planned_hours_to_date'], view['planned_labor_cost_to_date']),
                         (9_600, 16, 9_600))
        combined = forecast(('f/A', 'Инженер', 8), ('f/B', 'Инженер', 8))
        view = calculate_resources(combined, plan(), [], as_of='2026-11-16')
        self.assertEqual(view['finish'], '2026-11-17')
        self.assertEqual((view['planned_hours_to_date'], view['planned_labor_cost_to_date']), (8, 6_400))
        self.assertEqual(evaluate_plan_at(view['schedule'], '2026-11-12')['planned_hours_to_date'], 0)

    def test_dependency_wait_and_available_work_are_counted_in_productive_days(self):
        inputs = plan()
        inputs['dependencies'] = [dict(work='f/B', after='f/A', wait_days=1)]
        model = forecast(('f/A', 'Аналитик', 8), ('f/B', 'Инженер', 4), ('f/C', 'Инженер', 8), total=20)
        view = calculate_resources(model, inputs, [])
        self.assertEqual(view['finish'], '2026-11-18')
        self.assertEqual(evaluate_plan_at(view['schedule'], '2026-11-17')['planned_hours_to_date'], 16)
        inputs['dependencies'][0]['wait_days'] = 0
        model = forecast(('f/A', 'Аналитик', 4), ('f/B', 'Инженер', 4), total=8)
        self.assertEqual(calculate_resources(model, inputs, [])['finish'], '2026-11-13')

    def test_exact_pert_fraction_reserve_assignment_and_focus_factor(self):
        model = forecast(('f/W', 'Инженер', '40/3'), ('f/@R2', '', '2'), total=15.3)
        inputs = plan()
        inputs['teams'][0]['focus'] = '0,65'
        partial = calculate_resources(model, inputs, [])
        self.assertEqual((partial['finish'], partial['cost']), (None, None))
        self.assertEqual(partial['teams'][1]['known_demand_hours'], 13.3)
        inputs['assignments'] = [dict(work='f/@R2', role='Инженер')]
        view = calculate_resources(model, inputs, [])
        self.assertEqual((view['teams'][1]['daily_hours'], view['teams'][1]['capacity_hours']), (5.2, 26))
        self.assertEqual(view['cost'], 12_266.67)
        self.assertEqual(view['finish'], '2026-11-18')
        self.assertEqual(evaluate_plan_at(view['schedule'], '2026-11-20')['planned_hours_to_date'], 15.3)

    def test_missing_feature_is_not_filled_with_zero_or_projected_across_teams(self):
        model = forecast(('f/W', 'Инженер', 8), ('g/W', 'Инженер', None), total=None)
        model.update(known_hours=8, complete_count=1, feature_count=2)
        result = calculate_resources(model, plan(), [])
        self.assertEqual((result['known_hours'], result['total_hours'], result['finish'], result['cost']),
                         (8, None, None, None))
        engineer = result['teams'][1]
        self.assertEqual((engineer['known_demand_hours'], engineer['known_load_percent']), (8, 20))
        self.assertIsNone(engineer['load_percent'])
        self.assertIsNone(result['planned_hours_to_date'])
        self.assertEqual(result['known_cost'], 6_400)
        self.assertIsNone(result['actual_hours'])
        inputs = plan()
        inputs.update(teams=[team('Инженер', 0)], extra_cost=None)
        free_work = calculate_resources(model, inputs, [])
        self.assertEqual((free_work['cost'], free_work['known_cost']), (None, 0))

    def test_human_cost_api_tariffs_and_extra_keep_their_units(self):
        model = forecast(('f/W', 'Инженер', 4), total=4)
        inputs = plan()
        inputs.update(extra_cost='12.38', api=[dict(model='A', input_tokens=500_000, output_tokens=0,
                                                    input_rate='3.25', output_rate=None)])
        view = calculate_resources(model, inputs, [])
        self.assertEqual((view['planned_labor_cost'], view['api_cost'], view['cost']), (3_200, 1.63, 3_214.01))
        inputs['api'][0]['output_tokens'] = 1
        view = calculate_resources(model, inputs, [])
        self.assertEqual((view['api_cost'], view['cost'], view['known_cost']), (None, None, 3_214.01))
        inputs['teams'][0]['rate'] = None
        view = calculate_resources(model, inputs, [], '2026-11-14')
        self.assertIsNotNone(view['finish'])
        self.assertIsNone(view['planned_labor_cost_to_date'])

    def test_actuals_respect_dates_missing_values_and_recorded_currency(self):
        model = forecast(('f/W', 'Инженер', 8), total=8)
        rows = [fact('2026-11-13', hours=0, cost=0, currency='RUB'),
                fact('2026-11-15', hours=None, cost=10, currency='USD'),
                fact('2026-11-17', hours=8)]
        first = calculate_resources(model, plan(), rows, '2026-11-13')
        self.assertEqual((first['actual_hours'], first['actual_cost']), (0, 0))
        second = calculate_resources(model, plan(), rows, '2026-11-15')
        self.assertEqual((second['actual_hours'], second['actual_cost']), (None, None))
        self.assertEqual((second['known_actual_hours'], second['known_actual_cost']), (0, 0))
        self.assertEqual(calculate_resources(model, plan(), rows)['known_actual_hours'], 8)
        usd = ActualTotals()
        usd.add(rows[1])
        self.assertEqual((usd.summary()['actual_currency'], usd.summary()['actual_cost']), ('USD', 10))
        usd.add(rows[0])
        self.assertIsNone(usd.summary()['known_actual_cost'])

    def test_stale_dependency_cycle_and_invalid_fields_do_not_create_a_date(self):
        model = forecast(('f/A', 'Инженер', 4), ('f/B', 'Инженер', 4), total=8)
        inputs = plan()
        inputs['dependencies'] = [dict(work='f/B', after='f/X', wait_days=0)]
        stale = calculate_resources(model, inputs, [])
        self.assertIsNone(stale['finish'])
        self.assertEqual(stale['cost'], 6_400)
        self.assertTrue(any('f/X' in message for message in stale['issues']))
        inputs['dependencies'] = [dict(work='f/A', after='f/B', wait_days=0),
                                  dict(work='f/B', after='f/A', wait_days=0)]
        with self.assertRaisesRegex(ValueError, 'цикл'):
            calculate_resources(model, inputs, [])
        for change in (dict(target='2026-02-30'), dict(currency='123'), dict(weekdays=[True]),
                       dict(teams=[dict(team('инженер'), focus='1.1')]),
                       dict(api=[dict(model='A', input_tokens=1.5)])):
            with self.subTest(change=change), self.assertRaises(ValueError):
                calculate_resources(forecast(total=None), dict(plan(), **change), [])

    def test_absent_calendar_zero_capacity_and_missing_currency_are_independent(self):
        model = forecast(('f/W', 'Инженер', 8), total=8)
        self.assertIsNone(calculate_resources(model, {}, [])['working_days'])
        inputs = plan()
        inputs['teams'][0]['people'] = 0
        result = calculate_resources(model, inputs, [])
        self.assertIsNone(result['finish'])
        self.assertEqual(result['teams'][1]['capacity_hours'], 0)
        self.assertIsNone(result['teams'][1]['load_percent'])
        self.assertEqual(result['cost'], 6_400)
        inputs['teams'][0]['people'] = 1
        inputs['currency'] = ''
        result = calculate_resources(model, inputs, [])
        self.assertEqual(result['finish'], '2026-11-13')
        self.assertIsNone(result['cost'])
