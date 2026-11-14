"""Контекст этапа в рабочем HTTP-интерфейсе и граница записи решений."""
import http.client
import json
from pathlib import Path
from uuid import uuid4
import unittest
from urllib.parse import urlencode

from kit.dashboard.tests import test_runtime
from kit.project_vault import ProjectVault
from kit.project_workspace import section_paths


class StageHTTP(unittest.TestCase):
    setUp = test_runtime.RuntimeHTTP.setUp
    close = test_runtime.RuntimeHTTP.close
    request = test_runtime.RuntimeHTTP.request

    def bind(self):
        self.vault = ProjectVault.connect(self.root, Path(self.temp.name) / 'vault')
        self.vault.add('stage', 's', 'Документация')
        self.vault.add('feature', 'f', 'Цепочка проверок', 's')
        return self.vault

    def post(self, url, value, authorized=True):
        headers = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{self.server.server_port}'}
        if authorized:
            headers['X-Dashboard-UI-Token'] = self.server.ui_token
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request('POST', url, json.dumps(value).encode(), headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_entry_and_unbound_project(self):
        self.assertIn('href="/stage"', self.request('/')[2])
        for asset in ('/stage', '/stage.css', '/stage.js', '/stage-decisions.js', '/stage-resources.css', '/stage-resources.js'):
            self.assertEqual(self.request(asset)[0], 200)
        self.assertIsNone(json.loads(self.request('/api/stage')[2])['stage'])
        self.assertFalse((self.root / '.chief-project.json').exists())

    def test_real_sections_and_archived_source_for_selected_stage(self):
        vault = self.bind()
        path = section_paths(vault, 'f')['requirements']
        original = path.read_text(encoding='utf-8-sig')
        with path.open('a', encoding='utf-8') as note:
            note.write('\n- [ ] #рп #аналитик Уточнить основание\n- [x] #рп #аналитик Согласовать границу\n')
        status, _, body = self.request('/api/stage?stage=s')
        self.assertEqual(status, 200, body)
        view = json.loads(body)
        feature, = view['features']
        self.assertEqual(view['stage']['key'], 's')
        self.assertEqual(feature['tasks'], [dict(roles=['manager', 'analyst'], text='Уточнить основание', section='requirements')])
        self.assertEqual(feature['progress']['analyst'], dict(open=1, done=1))
        self.assertEqual(feature['progress']['all'], dict(open=1, done=1))
        url = feature['documents']['requirements']
        self.assertIn('Уточнить основание', self.request(url)[2])
        self.assertEqual(self.request(url + '&revision=1')[2], original)
        self.assertEqual(self.request(url + '&revision=200')[0], 404)
        self.assertIn('requirements', feature['saved_sections']['1'])
        with vault.locked() as db:
            db.execute("UPDATE revision_history SET document=json_remove(document,'$.documents') WHERE record_key='s'")
        legacy = json.loads(self.request('/api/stage?stage=s')[2])['stage']
        self.assertEqual(legacy['saved_sections']['1'], [])
        self.assertEqual(self.request(legacy['archive'])[0], 200)
        self.assertEqual(self.request('/api/stage?stage=s&stage=f')[0], 400)
        vault.add('stage', 'other', 'Другой этап')
        self.assertEqual(self.request(url.replace('stage=s','stage=other'))[0], 400)

    def test_discussion_and_decision_use_same_project_and_post_guard(self):
        vault = self.bind()
        event = dict(stage='s', project_id=vault.project_id, action='ask', feature='f', revision=1,
                     role='analyst', assignee='engineer', text='Как проверить границу?', operation_id=str(uuid4()))
        self.assertEqual(self.post('/api/stage/action', event, False)[0], 403)
        self.assertEqual(self.post('/api/stage/decision', event, False)[0], 403)
        for endpoint in ('/api/stage/action','/api/stage/decision'):
            self.assertEqual(self.post(endpoint, dict(event, project_id='other'))[0], 400)
        code, opened = self.post('/api/stage/action', event)
        self.assertEqual(code, 200, opened)
        self.assertEqual(self.post('/api/stage/action', event), (200, opened))
        view = json.loads(self.request('/api/stage?stage=s')[2])
        thread, = view['threads']
        self.assertEqual(thread['id'], opened['id'])
        proposal = dict(stage='s', project_id=vault.project_id, action='propose', request_id=str(uuid4()),
                        role='engineer', author='Инженер', reason='Нужна оценка работы',
                        fingerprint=view['decisions']['forecast']['fingerprint'])
        status, created = self.post('/api/stage/decision', proposal)
        self.assertEqual(status, 200, created)

    def test_resources_round_trip_and_inputs_for_an_observed_stage(self):
        from datetime import date
        vault = self.bind()
        view = lambda: json.loads(self.request('/api/stage?stage=s')[2])
        initial = view()
        self.assertEqual(initial['resources']['version'], 0)
        payload = dict(action='checkpoint', stage='s', date=date.today().isoformat(), author='РП',
                       source='Исходный срез', expected_version=0, request_id=str(uuid4()), project_id=vault.project_id,
                       fingerprint=initial['decisions']['forecast']['fingerprint'])
        url = '/api/stage/resource'
        self.assertEqual(self.post(url, payload, False)[0], 403)
        self.assertEqual(self.post(url, dict(payload, project_id='другой'))[0], 400)
        status, receipt = self.post(url, payload)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(self.post(url, payload), (200, receipt))
        history = view()['resources']['history']
        self.assertEqual(len(history), 1)
        snapshot = json.loads(self.request(url + '?stage=s&version=1')[2])
        self.assertEqual(snapshot['forecast']['feature_count'], 1)
        self.assertIsNone(snapshot['receipt']['metrics']['actual_hours'])
        notes = section_paths(vault, 'f')
        notes['effort'].write_text('| Работа / ID | Роль | Единица | O | M | P | Источник и допущение |\n'
                                   '| W | РП | чел·ч | 2 | 2 | 2 | Общее планирование |\n', encoding='utf-8')
        self.server.resource_observer.poll()
        self.server.resource_observer.poll()
        after = view()['resources']
        self.assertEqual(after['version'], 2)
        self.assertEqual(after['history'][-1]['metrics']['known_hours'], 2)
        self.assertEqual(self.request(url + '?stage=s&version=bad')[0], 400)

    def test_workspace_link_and_concurrent_edit(self):
        from urllib.parse import parse_qs, urlsplit
        import re
        vault = self.bind()
        self.server.resource_observer.poll()
        note = (vault.path / 'Монитор Чифа.md').read_text(encoding='utf-8-sig')
        link = re.search(r'\[Риски\]\(([^)]+)\)', note.split('## f\n')[1]).group(1)
        self.assertEqual(parse_qs(urlsplit(link).query), dict(stage=['s'], project=[vault.project_id],
                                                             feature=['f'], section=['risks'], team=['manager']))
        url = '/api/stage/document?stage=s&feature=f&section=risks'
        original = json.loads(self.request(url)[2])
        archived = json.loads(self.request(url + '&revision=1')[2])
        self.assertTrue(archived['readonly'])
        self.assertNotEqual(original['obsidian_url'], archived['obsidian_url'])
        payload = dict(stage='s', feature='f', section='risks', project_id=vault.project_id,
                       expected_hash=original['hash'], expected_revision=original['revision'],
                       author='Аналитик', text=original['text'] + '\nПроверено инженером.', operation_id=str(uuid4()))
        endpoint = '/api/stage/document'
        self.assertEqual(self.post(endpoint, payload, False)[0], 403)
        self.assertEqual(self.post(endpoint, dict(payload, project_id='other'))[0], 400)
        status, receipt = self.post(endpoint, payload)
        self.assertEqual(status, 200, receipt)
        self.assertEqual(self.post(endpoint, payload), (200, receipt))
        self.assertEqual(self.post(endpoint, dict(payload, operation_id=str(uuid4()), text='Другой текст'))[0], 409)
        current = json.loads(self.request(url)[2])
        self.assertEqual((current['last_receipt'], current['text']), (receipt, payload['text']))
        self.assertEqual(json.loads(self.request(url + '&revision=1')[2]), archived)

    def test_risk_question_preserves_its_source_after_a_second_editor_changes_it(self):
        import hashlib
        vault = self.bind()
        path = section_paths(vault, 'f')['risks']
        first = '### R1 · Перебои API\n\n| Поле | Значение |\n| --- | --- |\n| Мера | Уточнить квоту |\n'
        path.write_text(first, encoding='utf-8-sig')
        original_bytes = path.read_bytes()
        digest = hashlib.sha256(original_bytes).hexdigest()
        question = dict(project_id=vault.project_id, stage='s', feature='f', revision=1, role='manager',
                        action='ask', text='Какова квота?', assignee='engineer', operation_id=str(uuid4()),
                        risk_id='f/R1', risk_source='working', risk_hash=digest)
        self.assertEqual(self.post('/api/stage/action', dict(question, risk_hash='0' * 64))[0], 400)
        status, saved = self.post('/api/stage/action', question)
        self.assertEqual(status, 200, saved)
        path.write_text(first.replace('Уточнить квоту', 'Квота согласована'), encoding='utf-8')
        url = '/api/stage/discussion-source?' + urlencode(dict(stage='s', thread=saved['id']))
        self.assertEqual(self.request(url)[2], original_bytes.decode('utf-8-sig'))
        self.assertEqual(self.request(url.replace('stage=s', 'stage=another'))[0], 404)

    def test_only_real_test_event_can_address_its_log(self):
        event_id = uuid4().hex
        log = 'test-' + uuid4().hex + '.log'
        journal = self.metrics.task_dir('first')
        (journal / log).write_text('Результат замера', encoding='utf-8')
        event = dict(event_id=event_id, kind='test', scope='full', at=10, status='passed', log=log, tests=1)
        with (journal / 'events.jsonl').open('a', encoding='utf-8') as file:
            file.write(json.dumps(event) + '\n')
        self.assertEqual(json.loads(self.request('/api/stage/runs?task=first')[2])['runs'][0]['event_id'], event_id)
        url = '/api/stage/log?' + urlencode(dict(task='first',event=event_id))
        self.assertEqual(self.request(url)[2], 'Результат замера')
        self.assertEqual(self.request(url.replace('first','second'))[0], 404)
