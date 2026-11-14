"""Границы PERT, общие риски и утверждённый резерв в человеко-часах."""
from dataclasses import replace
from fractions import Fraction
import unittest

from kit.effort_estimate import RiskAllowance, WorkEstimate, display_hours, estimate_effort
from kit.estimate_documents import render_estimate


class EffortEstimateTests(unittest.TestCase):
    def setUp(self):
        self.work = WorkEstimate('W1', 'Инженер', 'чел·ч', '8', '12', '24', 'Весь цикл')
        self.risk = RiskAllowance('R1', ('W1',), 'в O/M/P', '')

    def test_three_points_and_included_risk_with_unknown_probability(self):
        value = estimate_effort([replace(self.work, included_risks=('R1',))], [self.risk])
        self.assertEqual(value.total, Fraction(40, 3))
        self.assertEqual(display_hours(value.rows[0].deviation), '2,7')
        self.assertEqual(value.reserve, 0)
        self.assertEqual(display_hours(value.total), '13,3')

    def test_reserve_shared_by_two_works_is_added_once_before_rounding(self):
        other = replace(self.work, key='W2')
        risk = replace(self.risk, treatment='отдельно', hours='1,5', works=('W1', 'W2'))
        result = estimate_effort([self.work, other], [risk])
        self.assertEqual(result.reserve, Fraction(3, 2))
        self.assertEqual(display_hours(result.total), '28,2')
        self.assertEqual(display_hours(Fraction('1.05')), '1,1')

    def test_repeated_risk_and_conflicting_treatment_identify_the_risk(self):
        included = replace(self.work, included_risks=('R1',))
        for works, risks in (
            ([self.work], [self.risk, self.risk]),
            ([included], [replace(self.risk, treatment='отдельно', hours='2')]),
            ([included], [replace(self.risk, hours='2')]),
            ([included, replace(included, key='W2')], [self.risk]),
            ([self.work], [replace(self.risk, works=('absent',))]),
            ([self.work], [replace(self.risk, treatment='без резерва', hours='2')]),
        ):
            with self.subTest(risks=risks), self.assertRaisesRegex(ValueError, 'R1'):
                estimate_effort(works, risks)

    def test_missing_points_risk_decision_and_explicit_zero(self):
        partial = replace(self.work, key='W2', likely='Нет данных')
        estimate = estimate_effort([self.work, partial], [replace(self.risk, treatment='отдельно')])
        self.assertIsNone(estimate.total)
        self.assertEqual(estimate.base, Fraction(40, 3))
        self.assertEqual(len(estimate.missing), 2)
        for treatment in ('без резерва', 'отдельно', 'в O/M/P'):
            zero = replace(self.work, optimistic='0', likely='0', pessimistic='0')
            self.assertEqual(estimate_effort([zero], [replace(self.risk, treatment=treatment, hours='0')]).total, 0)
        self.assertIsNone(estimate_effort([replace(self.work, included_risks=('R1',))], []).total)

    def test_invalid_middle_negative_nonfinite_wrong_unit_and_duplicate_work(self):
        for field, value in (('likely', '25'), ('likely', '7'), ('likely', 'NaN'),
                             ('pessimistic', 'inf'), ('optimistic', '-1'), ('unit', 'дней')):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, 'W1'):
                estimate_effort([replace(self.work, **{field: value})], [])
        with self.assertRaisesRegex(ValueError, 'уникальный'):
            estimate_effort([self.work, self.work], [])

    def test_document_parser_ignores_quoted_example_and_checks_broken_rows(self):
        effort = '| Работа / ID | Роль | Единица | O | M | P | Источник и допущение |\n'
        row = '| W1 | Инженер | чел·ч | 8 | 12 | 24 | Пример |\n'
        risk = '| ID риска | ID работ | Учёт | Резерв, чел·ч |\n| R1 | W1 | отдельно | 2 |'
        result = render_estimate('```markdown\n' + effort + row + '```\n' + effort + row, risk)
        self.assertIn('15,3 чел·ч', result)
        self.assertIn('Срок и стоимость', result)
        self.assertIn('15,3 чел·ч', render_estimate(effort + row.rstrip('|\n'), risk))
        for bad in (effort + row + effort, effort + row + '| W2 | Инженер | 8', 'Нет таблицы'):
            report = render_estimate(bad, risk)
            self.assertIn('Исправьте исходные данные', report)
            self.assertNotIn('15,3', report)
