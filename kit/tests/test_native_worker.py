"""Проверки границ одного инженерного хода и передачи проверенного текста из Responses."""
from codecs import BOM_UTF8
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from kit import project_docs
from kit.runtimes import native_responses, native_worker


class NativeWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source.py'
        self.source.write_bytes(BOM_UTF8 + b'FLAG = 1\r\n')
        (self.root / 'contract.md').write_bytes(b'FLAG is an integer.\n')

    def plan(self, agent='codex'):
        return native_worker.prepare(agent, self.root, 'source.py', 'Change the flag.', contracts=['contract.md'])

    def test_prepared_data_freezes_one_existing_source_and_contract(self):
        plan = self.plan()
        self.source.write_bytes(b'other change\n')
        self.assertEqual(plan['assigned']['text'], 'FLAG = 1\r\n')
        self.assertEqual([row['path'] for row in plan['contracts']], ['contract.md'])
        self.assertNotIn(str(self.root), native_worker._prompt(plan).decode())
        self.assertGreater(len(plan['assigned']['sha256']), 50)
        for invalid in ('', '../source.py', 'a/../source.py', 'C:\\file.py', 'source.py:stream',
                        '.git/config', '.metrics/run', 'missing.py', 'CON', '/source.py'):
            with self.subTest(file=invalid), self.assertRaises(ValueError):
                native_worker.prepare('codex', self.root, invalid, 'Edit.')

    def test_candidate_is_bound_to_original_path_and_preserves_encoding(self):
        for agent in ('codex','claude'):
            plan = self.plan(agent)
            accepted = dict(candidate=dict(path='source.py', content='FLAG = 2\n'))
            self.assertEqual(native_worker.candidate_bytes(plan, accepted), BOM_UTF8 + b'FLAG = 2\r\n')
            self.assertEqual(self.source.read_bytes(), BOM_UTF8 + b'FLAG = 1\r\n')
            for invalid in (dict(path='contract.md',content='Changed'), dict(path='source.py',content='x',extra={}),
                            [{'path':'source.py','content':'x'}], dict(path='source.py',content='\0')):
                with self.subTest(agent=agent,candidate=invalid), self.assertRaises(ValueError):
                    native_worker.candidate_bytes(plan, dict(candidate=invalid))

    def test_rejected_outputs_never_produce_candidate(self):
        plan = self.plan()
        sid = str(uuid4())
        def stream(value):
            return '\n'.join(map(json.dumps, [
                dict(type='thread.started',thread_id=sid),
                dict(type='item.completed',item=dict(type='agent_message',text=value)),
                dict(type='turn.completed',usage=dict(input_tokens=5,output_tokens=2))]))
        for text in ('{"path":"source.py","path":"contract.md","content":"x"}',
                     '{"path":"contract.md","content":"x"}', 'missing json'):
            with self.assertRaises(ValueError):
                native_worker.parse_result(plan, stream(text), 0)
        with self.assertRaises(ValueError):
            native_worker.parse_result(plan, stream('{"path":"source.py","content":"x"}'), 1)
        with self.assertRaises(ValueError):
            native_worker.parse_result(plan, json.dumps(dict(type='thread.started',thread_id=sid)), 0)

    def test_claude_must_attest_empty_tool_and_mcp_lists(self):
        plan = self.plan('claude')
        sid = str(uuid4())
        answer = dict(type='result',subtype='success',session_id=sid,is_error=False,
                      result='{"path":"source.py","content":"FLAG = 2\\n"}')
        for init in ({}, dict(tools=['Read'],mcp_servers=[]),dict(tools=[],mcp_servers=['extra'])):
            stream = [dict(type='system',subtype='init',session_id=sid,**init),answer]
            with self.assertRaises(ValueError):
                native_worker.parse_result(plan,'\n'.join(map(json.dumps,stream)),0)

    def test_chief_returns_text_and_cannot_supply_a_candidate(self):
        plan = native_worker.prepare('claude', self.root, None, 'Summarize.', role='chief')
        self.assertIsNone(native_worker.candidate_bytes(plan, dict(text='OK')))
        with self.assertRaises(ValueError):
            native_worker.candidate_bytes(plan, dict(text='OK', candidate={}))
        with self.assertRaises(ValueError):
            native_worker.prepare('codex', self.root, 'source.py', 'Write.',role='chief')

    def test_profiles_and_custom_hooks_are_not_inherited(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'NODE_OPTIONS':'operator-hook',
                                                               'ANTHROPIC_API_KEY':'upstream-only',
                                                               'CODEX_HOME':'operator-config'}):
            env = native_worker._environment(Path(tmp))
        self.assertNotIn('NODE_OPTIONS',env)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotEqual(env['CODEX_HOME'], 'operator-config')

    def test_public_probe_separates_direct_delivery_and_managed_team(self):
        for agent in ('claude', 'codex'):
            transport = native_worker.TRANSPORTS[agent]
            flags = dict(available=True, worker_guarantees={'assigned_file_only':False})
            args = project_docs._parser().parse_args(['probe','--agent',agent])
            with patch.object(transport, 'probe', return_value=flags), patch.object(
                    transport, '_executable', return_value='test'), patch.object(native_worker, '_check_runtime') as cli:
                with patch('kit.runtimes.native_process.supported', return_value=True):
                    result = project_docs.execute(args)
                    self.assertTrue(result['managed_team']['available'])
                    self.assertFalse(result['managed_team']['model_checked'])
                    self.assertFalse(result['worker_guarantees']['assigned_file_only'])
                    cli.side_effect = ValueError('Missing flag')
                    self.assertFalse(project_docs.execute(args)['managed_team']['cli_compatible'])
                cli.side_effect = None
                with patch('kit.runtimes.native_process.supported', return_value=False):
                    result = project_docs.execute(args)['managed_team']
                    self.assertTrue(result['cli_compatible'])
                    self.assertFalse(result['available'])


class TextResponsesTests(unittest.TestCase):
    def test_broker_replaces_tools_and_rejects_actions_before_returning_any_frames(self):
        captures = []
        def message():
            return dict(type='message',role='assistant',content=[dict(type='output_text',text='OK')])
        responses = [
            dict(id='resp_ok', status='completed',output=[message()],usage=dict(input_tokens=22,output_tokens=1)),
            dict(id='resp_forbidden',status='completed',output=[message(),dict(type='function_call',name='dummy')]),
            None]
        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                captures.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                response = responses.pop(0)
                if response is None:
                    self.send_error(429)
                    return
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        client = build_opener(ProxyHandler({}))
        with ThreadingHTTPServer(('127.0.0.1',0),Model) as model:
            thread = Thread(target=model.serve_forever,daemon=True)
            thread.start()
            try:
                origin = 'http://127.0.0.1:' + str(model.server_port)
                with native_responses.text_responses(origin+'/v1',None,'approved') as (local,token):
                    body = json.dumps(dict(input='task',tools=[{'type':'function','name':'write'}],
                                           previous_response_id='unrelated',model='other')).encode()
                    submit = Request(local+'/responses', data=body,headers={'Authorization':'Bearer '+token})
                    with client.open(submit,timeout=3) as reply:
                        events = [json.loads(line[6:]) for line in reply.read().splitlines() if line.startswith(b'data: ')]
                    last = events[-1]['response']
                    self.assertEqual(last['usage']['input_tokens'],22)
                    self.assertEqual(last['output'][0]['content'][0]['text'],'OK')
                    self.assertEqual(captures[0]['tools'], [])
                    self.assertEqual(captures[0]['tool_choice'],'none')
                    self.assertEqual(captures[0]['model'],'approved')
                    self.assertNotIn('previous_response_id',captures[0])
                    with self.assertRaises(HTTPError) as blocked:
                        client.open(submit,timeout=3)
                    self.assertEqual(blocked.exception.code,502)
                    blocked.exception.close()
                    with self.assertRaises(HTTPError) as quota:
                        client.open(submit,timeout=3)
                    self.assertEqual(quota.exception.code,429)
                    quota.exception.close()
                    with self.assertRaises(HTTPError) as stranger:
                        client.open(Request(local+'/responses',data=body),timeout=3)
                    self.assertEqual(stranger.exception.code,403)
                    stranger.exception.close()
            finally:
                model.shutdown()
                thread.join()

    def test_provider_requires_tls_or_a_local_address(self):
        for unsafe in ('http://example.org/v1','https://x:y@site/v1','file:///tmp/s','https://api.test/#f'):
            with self.assertRaises(ValueError):
                native_responses.endpoint(unsafe)
        self.assertEqual(native_responses.endpoint('http://127.0.0.1:1234/v1/'),'http://127.0.0.1:1234/v1')
