"""Вердикт проверки связан с замороженным тестом и точной копией кандидата."""
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit import native_candidate_check as check


class CandidateCheckTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.state = SimpleNamespace(directory=self.root/'journal')
        job = str(uuid4())
        self.workspace = self.state.directory/'workspaces'/job
        self.workspace.mkdir(parents=True)
        self.file = self.workspace/'count.py'
        self.file.write_text('total = 7\n', encoding='utf-8')
        self.digest = sha256(self.file.read_bytes()).hexdigest()
        self.job = dict(id=job, workspace=str(self.workspace), assigned_file='count.py',candidate_hash=self.digest)
        self.plan = {'assigned':{'path':'count.py'}, 'verification':{'command':['python','-m','unittest','test_count']}}
        self.freeze_test('import unittest\nimport count\nclass Sum(unittest.TestCase):\n    def test_total(self):\n        self.assertEqual(count.total,8)\n')

    def freeze_test(self, body):
        self.test_file = self.workspace/'test_count.py'
        self.test_file.write_text(body,encoding='utf-8')
        self.plan['contracts'] = [{'path':'test_count.py', 'sha256':sha256(self.test_file.read_bytes()).hexdigest()}]

    def verify(self):
        return check.verify(self.state,self.job,self.plan,boundary=str(uuid4()),admission=nullcontext,should_stop=lambda:False)

    def test_source_or_contract_changed_refuses_to_run(self):
        with patch.object(check,'_run_check',side_effect=AssertionError('процесс не должен запускаться')):
            self.file.write_bytes(b'other')
            self.assertEqual(self.verify()['verdict'],'blocked')
            self.file.write_text('total = 7\n',encoding='utf-8')
            self.test_file.write_bytes(b'other')
            self.assertEqual(self.verify()['verdict'],'blocked')

    def test_command_selects_only_frozen_unittest(self):
        names = ['test_count.py']
        for cmd in (['echo','-m','unittest','test_count'], ['python','-c','pass'],
                    ['python','-m','unittest','test_else'], ['python','-m','unittest','../outside.py']):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                check.validate_command(cmd,names)
        self.assertEqual(check.validate_command(['python','-m','unittest','test_count.Sum.test_total'],names)[-1],
                         'test_count.Sum.test_total')

    def test_real_unittest_cannot_write_to_main_project_or_its_frozen_test(self):
        if sys.platform != 'win32':
            from kit.runtimes import native_process
            if not native_process.supported():
                self.skipTest('Для проверки записи нужна доступная группа systemd с Landlock.')
        external = self.root / 'source_untouched.txt'
        external.write_text('исходник', encoding='utf-8')
        self.freeze_test(
            'from pathlib import Path\nimport unittest\nimport count\n'
            'class Sum(unittest.TestCase):\n'
            '    def test_permissions_and_result(self):\n'
            '        for path in (Path(__file__), Path(' + repr(str(external)) + ')):\n'
            '            with self.assertRaises(PermissionError):\n'
            '                path.write_text("изменено", encoding="utf-8")\n'
            '        self.assertEqual(count.total, 8)\n')
        red = self.verify()
        self.assertEqual((red['verdict'], red['failures']), ('failed',1), red)
        self.file.write_text('total = 8\n', encoding='utf-8')
        self.job['candidate_hash'] = sha256(self.file.read_bytes()).hexdigest()
        green = self.verify()
        self.assertEqual((green['verdict'],green['tests']),('passed',1),green)
        self.assertEqual((red['test_hashes'],red['test_name']), (green['test_hashes'],green['test_name']))
        self.assertNotEqual(red['candidate_hash'],green['candidate_hash'])
        self.assertEqual(external.read_text(encoding='utf-8'), 'исходник')

    def test_infrastructure_is_separate_from_assertion_failure(self):
        counts = dict(tests=1,failures=0,errors=0,skipped=0,unexpected_successes=0,expected_failures=0,
                      observed=True,load_errors=False,test_name='test_case.Sum.test_total')
        for updates, expected in [({},'passed'),({'failures':1},'failed'),({'errors':1},'blocked'),
                                  ({'observed':False},'blocked'),({'skipped':1},'blocked'),
                                  ({'load_errors':True},'blocked'),({'tests':0},'blocked')]:
            with self.subTest(updates=updates), patch.object(check,'_run_check',return_value=dict(counts,**updates)):
                self.assertEqual(self.verify()['verdict'],expected)
