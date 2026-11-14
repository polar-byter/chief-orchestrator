"""Права и адреса команд рабочей страницы нативной команды."""
import http.client
import json
from base64 import b64decode
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from kit.dashboard import launcher, runtime
from kit.native_team_state import NativeTeamState
from kit.task_metrics import TaskMetrics


class NativeTeamHTTP(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.server = runtime.DashboardServer(self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .03}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.project = launcher.project_id(self.root)

    def close(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def request(self, method, url, body=None, *, authorized=True, origin=None):
        headers = {}
        if body is not None:
            headers = {'Content-Type':'application/json', 'Origin': origin or self.server.state()['base_url']}
            if authorized:
                headers['X-Dashboard-UI-Token'] = self.server.ui_token
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, url, json.dumps(body).encode() if body is not None else None, headers=headers)
            response = conn.getresponse()
            content = response.read().decode()
            return response.status, json.loads(content) if 'application/json' in response.getheader('Content-Type') else content
        finally:
            conn.close()

    def new_team(self, task='native', agent='codex'):
        TaskMetrics(self.root).start(task, title='Команда проверок')
        team = NativeTeamState(self.root, task)
        team.initialize(agent)
        return team

    def test_partial_is_available_by_explicit_request_after_the_attempt_is_removed(self):
        team = self.new_team()
        job_id = str(uuid4())
        work = team.directory / 'workspaces' / job_id
        work.mkdir(parents=True)
        (work / 'invoice.py').write_bytes(b'price = 10\n')
        team.enqueue(role='executor', instructions='Проверить цену', assigned_file='invoice.py',
                     workspace=work, operation_id=job_id)
        job = team.claim()
        with team.admit(job_id, job['boundary']):
            pass
        draft = b'# private draft\r\nprice = 20\r\n'
        stop = team.begin_provider_stop(job_id, job['boundary'], 'api_error', partial=draft, model='local-a')
        team.confirm_stop(stop['generation'], stop['active'])
        team.resolve(job_id, 'discard')
        view = self.request('GET', '/api/task?task=native')[1]
        self.assertNotIn('private draft', json.dumps(view))
        self.assertEqual(view['native_team']['jobs'][0]['provider_incidents'][0]['partial_size'], len(draft))
        body = dict(project=self.project, task='native', job_id=job_id, attempt=1)
        path = '/api/native-partial'
        self.assertEqual(self.request('POST', path, body, authorized=False)[0], 403)
        self.assertEqual(self.request('POST', path, dict(body, project='different'))[0], 400)
        self.assertEqual(self.request('POST', path, dict(body, attempt=2))[0], 400)
        status, content = self.request('POST', path, body)
        self.assertEqual((status, b64decode(content['content'])), (200, draft))

    def post(self, action, generation, *, task='native', **extra):
        return self.request('POST', '/api/native-team', dict(task=task, action=action, project=self.project,
                                                            generation=generation, **extra))

    def test_project_settings_render_and_switch_without_any_task(self):
        mode = '/api/chief-mode'
        initial = self.request('GET', mode)[1]
        self.assertEqual((initial['enabled'], initial['project']), (False, self.project))
        self.assertEqual(self.request('GET','/api/tasks'), (200, []))
        self.assertIn('id="chief-mode-toggle"', self.request('GET','/')[1])
        self.assertIn('native-team-jobs', self.request('GET','/native-team.js')[1])
        data = dict(project=self.project, enabled=True)
        for authorized, origin in ((False, None), (True, 'http://invalid.test')):
            self.assertEqual(self.request('POST', mode, data, authorized=authorized, origin=origin)[0], 403)
        self.assertEqual(self.request('POST', mode, dict(data, enabled='true'))[0], 400)
        self.assertEqual(self.request('POST', mode, dict(data, project='wrong'))[0], 400)
        self.assertEqual(self.request('POST', mode+'?target=other', data)[0], 400)
        kit = self.root / 'kit'
        kit.mkdir()
        (kit / 'run_opencode.py').write_text('# fixture', encoding='utf-8')
        manifest = self.root / '.metrics/.kit-installation.json'
        manifest.parent.mkdir(exist_ok=True)
        manifest.write_text('{"format":1,"files":{}}', encoding='utf-8')
        self.assertFalse(self.request('GET', mode)[1]['enabled'])
        files = {name: '0' * 64 for name in ('AGENTS.md', 'opencode.jsonc', 'kit/run_opencode.py')}
        manifest.write_text(json.dumps({'format': 1, 'files': files}), encoding='utf-8')
        self.assertTrue(self.request('GET', mode)[1]['enabled'])
        self.assertFalse(self.request('POST', mode, dict(data, enabled=False))[1]['enabled'])
        self.assertTrue(self.request('POST', mode, data)[1]['enabled'])

    def test_native_data_has_only_public_fields_and_does_not_read_external_sessions(self):
        team = self.new_team()
        job = team.enqueue(role='chief', workspace=self.root, instructions='PRIVATE-PROMPT')
        active = team.claim()
        args = job['id'], active['boundary']
        with team.admit(*args):
            pass
        team.checkpoint(*args, candidate=None, result={'text':'PRIVATE-REPLY', 'credential':'SECRET'})
        team.commit(*args)
        with patch.object(self.server.reader, 'view', side_effect=AssertionError('Поход в стороннюю сессию')):
            code, result = self.request('GET','/api/task?task=native')
        self.assertEqual(code, 200, result)
        public = result['native_team']
        self.assertEqual((public['agent'],public['jobs'][0]['status']), ('codex','completed'))
        serialized = json.dumps(result)
        for private in ('PRIVATE-PROMPT','PRIVATE-REPLY','SECRET',str(self.root),'instructions','contracts'):
            self.assertNotIn(private, serialized)
        self.assertEqual(self.request('GET','/api/tasks')[1][0]['task'], 'native')

    def test_selected_native_controls_reject_stale_actions_and_preserve_other_teams(self):
        selected = self.new_team()
        other = self.new_team('other', 'claude')
        metrics = TaskMetrics(self.root)
        metrics.start('legacy', title='Отдельная задача')
        selected.enqueue(role='chief', workspace=self.root, instructions='Вопрос')
        target = selected.claim()
        queued = selected.enqueue(role='chief', workspace=self.root, instructions='Дальше')
        address = '/api/task/control'
        with patch.object(self.server.controls, 'context', side_effect=AssertionError('Использован прежний шлюз')):
            code, paused = self.request('POST',address,dict(task='native',action='pause'))
        self.assertEqual((code, paused.get('status')), (202,'paused'))
        view = self.request('GET','/api/task?task=native')[1]['native_team']
        self.assertEqual(self.request('GET','/api/task/control?task=native')[1]['status'], 'paused')
        self.assertEqual(other.view()['status'],'running')
        self.assertEqual(self.request('GET','/api/task/control?task=legacy')[1], {})
        generation = view['generation']
        proof = dict(job_id=target['id'], attempt=target['attempt'], boundary=target['boundary'])
        self.assertEqual(self.post('discard',generation-1,**proof)[0],400)
        self.assertEqual(self.post('discard',generation,**dict(proof, attempt=42))[0],400)
        self.assertEqual(self.post('discard',generation,task='other',**proof)[0],400)
        self.assertEqual(self.post('discard',generation,**proof)[0],200)
        self.assertEqual(self.post('drop',generation,job_id=queued['id'],attempt=0,boundary=None)[0],200)
        self.assertEqual(other.view()['generation'],1)
        self.assertEqual(self.request('POST',address,dict(task='native',action='delete'))[0],400)

    def test_native_queue_rejects_unscoped_requests(self):
        self.new_team()
        data = dict(project=self.project, task='native', action='clear', generation=1)
        url = '/api/native-team'
        self.assertEqual(self.request('POST',url,data,authorized=False)[0],403)
        self.assertEqual(self.request('POST',url,data,origin='http://outside.test')[0],403)
        for changes in (dict(task='../native'), dict(project='wrong'), dict(generation=True), dict(extra='value'),
                        dict(action='recover', boundary=None), dict(action='drop'), dict(action='use-shell')):
            with self.subTest(changes=changes):
                self.assertEqual(self.request('POST',url,dict(data,**changes))[0],400)

    def test_launcher_accepts_project_without_journal_and_still_checks_named_task(self):
        with patch.object(launcher, 'health', return_value={'version': launcher.VERSION}), patch.object(launcher, 'read_state',
                return_value=dict(base_url='http://127.0.0.1:34567', pid=1, instance_id='fixture')):
            view = launcher.ensure_dashboard(self.root, open_browser=False)
        self.assertEqual(view['url'], 'http://127.0.0.1:34567/?project=' + self.project)
        with self.assertRaisesRegex(ValueError,'Register'):
            launcher.ensure_dashboard(self.root,'unknown', open_browser=False)
