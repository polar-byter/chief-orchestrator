"""Проверка сводного реестра по рабочим заметкам и решению о резерве."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths
from kit.stage_decisions import decision_view
from kit.stage_risks import risk_in_note, risk_register


ALLOCATION = '| ID риска | ID работ | Учёт | Резерв, чел·ч |\n'
WORKS = '| Работа / ID | Роль | Единица | O | M | P | Источник и допущение | Риски в O/M/P |\n'
GROUPS = '| Общее событие | Риски фич | Где учтено |\n'


class RiskRegisterTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name) / 'проект'
        root.mkdir()
        self.vault = ProjectVault.connect(root)
        self.stage = 'stage'
        self.vault.add('stage', self.stage, 'Поставка')

    def feature(self, key, risks, works='| W1 | Инженер | чел·ч | 2 | 2 | 2 | Обмер | |\n', stage=None):
        self.vault.add('feature', key, key, stage or self.stage)
        notes = section_paths(self.vault, key)
        notes['effort'].write_text(WORKS + works, encoding='utf-8')
        notes['risks'].write_text(risks, encoding='utf-8')
        return notes['risks']

    def view(self):
        forecast = decision_view(self.vault, self.stage)['forecast']
        return risk_register(self.vault, self.stage, forecast)

    def test_partial_stage_exact_pert_separate_reserve_and_unknown_probability(self):
        notes = ('### R1 · Отказ API\n| Поле | Значение |\n'
                 '| Причина | Ограничение запросов |\n| Вероятность | Неизвестна |\n'
                 '| Владелец | РП |\n| Мера | Получить ответ инженера |\n'
                 '| Дата пересмотра | 2026-11-15 |\n\n' + ALLOCATION +
                 '| R1 | W1 | в O/M/P | |\n| R2 | W1 | отдельно | 2 |\n')
        self.feature('api', notes, '| W1 | Инженер | чел·ч | 8 | 12 | 24 | Обмер | R1 |\n')
        self.vault.add('feature', 'todo', 'Позже', self.stage)
        docs = self.view()
        self.assertEqual((docs['base_hours'], docs['reserve_hours'], docs['known_hours'], docs['total_hours']),
                         (13.3, 2, 15.3, None))
        first, second = docs['rows']
        self.assertEqual((first['title'], first['probability'], first['owner'], first['added_hours']),
                         ('Отказ API', None, 'РП', 0))
        self.assertEqual((second['counted_at'], second['added_hours'], second['works']), ('api/R2', 2, ['W1']))
        self.assertEqual(first['revision'], 1)
        self.assertEqual(len(first['source_hash']), 64)

    def test_explicit_common_cause_and_evidence_is_scoped_to_the_stage(self):
        for key in ('one', 'two', 'third'):
            self.feature(key, ALLOCATION + '| R1 | W1 | отдельно | 3 |\n')
        self.vault.add('stage', 'other', 'Другой этап')
        self.feature('outside', ALLOCATION + '| R9 | W1 | отдельно | 10 |\n', stage='other')
        section_paths(self.vault, self.stage)['risks'].write_text(GROUPS +
            '| Единая квота | one/R1, two/R1 | two/R1 |\n', encoding='utf-8')
        summary = self.view()
        risks = {r['id']: r for r in summary['rows']}
        self.assertEqual(set(risks), {'one/R1', 'two/R1', 'third/R1'})
        self.assertEqual((risks['one/R1']['added_hours'], risks['one/R1']['counted_at']), (0, 'two/R1'))
        self.assertEqual((risks['two/R1']['added_hours'], risks['two/R1']['group']), (3, 'Единая квота'))
        self.assertIsNone(risks['third/R1']['group'])
        self.assertEqual(summary['reserve_hours'], sum(r['added_hours'] for r in risks.values()))

    def test_template_legacy_labels_and_unallocated_stage_risk(self):
        path = section_paths(self.vault, self.stage)['risks']
        original = path.read_text(encoding='utf-8-sig')
        self.assertFalse(risk_in_note(original, 'R-01'))
        new = ('\n### R-04 · Среда недоступна\n| Поле | Значение |\n'
               '| Вероятность / категория и основание | 0% по последнему замеру |\n'
               '| Источник, выборка, период и условия | Приёмка 14.11 |\n'
               '| Владелец и исполнитель меры | Инженер |\n'
               '| Мера, срок проверки и сигнал срабатывания | Два независимых запуска |\n'
               '| Наблюдение после меры и остаточный риск | Низкий |\n'
               '| ID работ | W2, W3 |\n')
        path.write_text(original + new + '\n```md\n### R7 · Пример\n```\n', encoding='utf-8')
        row, = self.view()['rows']
        self.assertEqual((row['id'], row['probability'], row['residual'], row['works']),
                         ('stage/R-04', '0% по последнему замеру', 'Низкий', ['W2', 'W3']))
        self.assertIsNone(row['added_hours'])
        self.assertTrue(risk_in_note(new, 'R-04'))
        self.assertFalse(risk_in_note(new, 'R-0'))
        self.assertFalse(risk_in_note('```\n' + new + '\n```', 'R-04'))

    def test_bad_section_never_hides_other_sources_or_spends_an_invalid_reserve(self):
        self.feature('bad', ALLOCATION + '| R1 | W1 | в O/M/P | 2 |\n')
        self.feature('good', ALLOCATION + '| R2 | W1 | отдельно | 4 |\n')
        self.feature('notes', '### Broken · Повтор поля\n| Поле | Значение |\n| Владелец | РП |\n'
                     '| Владелец | Инженер |\n\n### QA · Проверка\n| Поле | Значение |\n| Владелец | Аналитик |\n')
        report = self.view()
        by_id = {item['id']: item for item in report['rows']}
        self.assertIsNone(by_id['bad/R1']['added_hours'])
        self.assertEqual(by_id['good/R2']['added_hours'], 4)
        self.assertEqual(by_id['notes/QA']['owner'], 'Аналитик')
        self.assertTrue(any('Broken' in issue for issue in report['issues']))

    def test_source_changed_during_refresh_never_labels_new_document_with_old_claim(self):
        source = self.feature('api', ALLOCATION + '| R1 | W1 | отдельно | 1 |\n')
        forecast = decision_view(self.vault, self.stage)['forecast']
        source.write_text(ALLOCATION + '| R1 | W1 | отдельно | 8 |\n', encoding='utf-8')
        report = risk_register(self.vault, self.stage, forecast)
        row, = report['rows']
        self.assertEqual(row['declared_hours'], 8)
        self.assertIsNone(row['added_hours'])
        self.assertTrue(any('после расчёта' in msg for msg in report['issues']))
