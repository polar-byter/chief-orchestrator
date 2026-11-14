"""Передача файла от исполнителя исправителю через обычную очередь Чифа."""
from codecs import BOM_UTF8
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from kit import chief_mode, native_candidate_check, native_repair, native_team
from kit.native_team_state import NativeTeamState


class RepairDispatchTests(unittest.TestCase):
    def test_dispatch_hands_off_exact_rejected_file_and_accepts_only_the_fix(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            team = NativeTeamState(root, 'quota')
            team.initialize('claude')
            mode = root / chief_mode.STATE
            mode.parent.mkdir(parents=True, exist_ok=True)
            mode.write_bytes(chief_mode.encoded_state({'status': 'on'}))
            source = root / 'quota.py'
            source.write_bytes(b'status = None\r\n')
            test = root / 'test_quota.py'
            contract = BOM_UTF8 + b'import quota\r\n'
            test.write_bytes(contract)
            rejected = b'status = 428\r\n'
            accepted = b'status = 429\r\n'
            requirement = 'Распознать исчерпание квоты.'
            original = native_team.enqueue(root, team.task, 'executor', requirement, assigned_file=source.name,
                                           contracts=[test.name], max_attempts=3, fixer_attempts=2,
                                           test_command=['python', '-m', 'unittest', 'test_quota'])
            plans = []
            checked = []

            def produce_candidate(state, job):
                plan = json.loads(native_repair.assignment_path(state, job['id']).read_bytes())
                plans.append(plan)
                with state.admit(job['id'], job['boundary']):
                    pass
                candidate = accepted if job['role'] == 'bug_fixer' else rejected
                state.checkpoint(job['id'], job['boundary'], candidate=candidate, result={})
                state.commit(job['id'], job['boundary'])

            def check_snapshot(folder, selected, assigned, *, admission, should_stop, boundary):
                with admission():
                    pass
                snapshot = folder / 'source'
                self.assertEqual((snapshot / test.name).read_bytes(), contract)
                value = (snapshot / assigned).read_bytes()
                checked.append(value)
                return dict(tests=1, failures=int(value != accepted), errors=0, skipped=0,
                            expected_failures=0, unexpected_successes=0, observed=True, load_errors=False,
                            test_name='test_quota.Quota.test_exhausted')

            with patch.object(native_team, '_execute', side_effect=produce_candidate), \
                    patch.object(native_candidate_check, '_run_check', side_effect=check_snapshot), \
                    patch.object(native_team.native_process, 'stop_and_confirm', return_value=True):
                native_team.drain(root, team.task)
                view = native_team.drain(root, team.task)

            self.assertEqual(checked, [rejected, accepted])
            executor, fixer = plans
            self.assertEqual(fixer['assigned']['text'].encode(), rejected)
            self.assertEqual(fixer['verification'], executor['verification'])
            self.assertEqual(fixer['instructions'], requirement)
            self.assertEqual(fixer['contracts'], executor['contracts'])
            transfer = fixer['repair']
            self.assertEqual(transfer['previous_job'], original['id'])
            self.assertEqual(transfer['rejected_sha256'], sha256(rejected).hexdigest())
            self.assertEqual((transfer['remaining_total'], transfer['remaining_fixer']), (2, 2))
            self.assertEqual(transfer['test_name'], 'test_quota.Quota.test_exhausted')
            with self.assertRaises(ValueError):
                native_team.integrate(team, original['id'])
            self.assertEqual(source.read_bytes(), b'status = None\r\n')
            successor, = [job for job in view['jobs'] if job['role'] == 'bug_fixer']
            native_team.integrate(team, successor['id'])
            self.assertEqual(source.read_bytes(), accepted)
            self.assertEqual(test.read_bytes(), contract)
