"""Классификация конечного отказа API и копия полученного до него файла."""
from codecs import BOM_UTF8
from io import BytesIO
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from kit.runtimes.native_provider_events import observe
from kit.runtimes.native_responses import _file_before_failure, text_responses


class ObservationTests(unittest.TestCase):
    def observe(self, agent, *events, code=1, assigned=True):
        plan = {'agent': agent, 'assigned': {'path': 'quota.py', 'bom': True, 'crlf': True} if assigned else None}
        return observe(plan, b'\n'.join(json.dumps(event).encode() for event in events), code)

    @staticmethod
    def codex_file(text):
        return dict(type='item.completed', item=dict(type='agent_message', text=text))

    def test_codex_uses_the_terminal_fault_and_keeps_only_a_valid_assigned_file(self):
        file = self.codex_file(json.dumps({'path': 'quota.py', 'content': 'first\n'}))
        wrong = self.codex_file('{"path":"test.py","content":"private"}')
        duplicate = self.codex_file('{"path":"private","path":"quota.py","content":"ignore"}')
        failed = dict(type='turn.failed', error={'message': 'unexpected status 503 Service Unavailable: private'})
        later = self.codex_file('{"path":"quota.py","content":"after the failure"}')
        result = self.observe('codex', file, wrong, duplicate, failed, later)
        self.assertEqual(result, {'reason': 'api_error', 'partial': BOM_UTF8 + b'first\r\n'})
        self.assertEqual(self.observe('codex', file, failed, assigned=False)['partial'], None)

    def test_codex_temporary_signal_does_not_override_success_or_a_local_error(self):
        recovering = dict(type='error', message='unexpected status 429 Too Many Requests')
        for events, code in (([recovering, {'type': 'turn.completed'}], 0),
                             ([{'type': 'turn.failed', 'error': {'message': 'assertion failed at line 429'}}], 1),
                             ([recovering], 0), ([{'type': 'turn.failed', 'error': 'config missing'}], 1)):
            with self.subTest(events=events):
                self.assertIsNone(self.observe('codex', *events, code=code))
        from_candidate = self.codex_file('{"path":"quota.py","content":"API Error: 429"}')
        self.assertIsNone(self.observe('codex', from_candidate, code=1))
        failed = dict(type='turn.failed', error={'message': 'unexpected status 401 Unauthorized: account id 429'})
        self.assertEqual(self.observe('codex', failed)['reason'], 'api_error')

    def test_gateway_knows_if_a_502_came_from_upstream_or_local_validation(self):
        failed = {'type': 'turn.failed', 'error': {'message': 'unexpected status 502 Bad Gateway'}}
        def marker(outcome):
            return {'type': 'chief.gateway_result', 'outcome': outcome}
        self.assertIsNone(self.observe('codex', failed, marker('invalid_response')))
        self.assertIsNone(self.observe('codex', failed, marker('ok')))
        for reason in ('limit', 'api_error'):
            self.assertEqual(self.observe('codex', failed, marker(reason)), dict(reason=reason, partial=None))
            self.assertIsNone(self.observe('codex', failed, {'type': 'turn.completed'}, marker(reason), code=0))

    def test_only_final_confirmed_gateway_marker_can_supply_a_new_partial(self):
        old = self.codex_file('{"path":"quota.py","content":"old"}')
        failed = dict(type='turn.failed', error={'message': 'unexpected status 503 Service Unavailable'})
        marker = dict(type='chief.gateway_result', outcome='api_error', partial_text='{"path":"quota.py","content":"new"}')
        self.assertEqual(self.observe('codex', old, failed, marker)['partial'], BOM_UTF8 + b'new')
        self.assertEqual(self.observe('codex', old, marker, failed)['partial'], BOM_UTF8 + b'old')
        stale = dict(marker, partial_text='{"path":"wrong.py","content":"private"}')
        self.assertEqual(self.observe('codex', old, failed, stale)['partial'], BOM_UTF8 + b'old')
        self.assertIsNone(self.observe('codex', failed, stale)['partial'])
        self.assertIsNone(self.observe('codex', failed, dict(marker, outcome='ok')))

    def test_claude_discriminates_terminal_api_evidence_and_quota_from_billing(self):
        last = dict(type='result', subtype='success', is_error=True, result='private provider error')
        init = dict(type='system', subtype='init', model='claude-test')
        for kind, reason in [('authentication_failed', 'api_error'), ('billing_error', 'api_error'),
                             ('rate_limit', 'limit'), ('server_error', 'api_error')]:
            issue = dict(type='assistant', error=kind, message={'content': [{'type': 'text', 'text': 'private'}]})
            self.assertEqual(self.observe('claude', init, issue, last), dict(reason=reason, partial=None, model='claude-test'))
            self.assertIsNone(self.observe('claude', init, issue, dict(last, is_error=False), code=0))
        self.assertIsNone(self.observe('claude', init, dict(last, subtype='error_max_turns', result='HTTP 429')))
        self.assertIsNone(self.observe('claude', init, dict(last, result='Unrelated note in line 429')))
        self.assertNotIn('model', self.observe('claude', dict(init, model='bad model\nkey!'),
                                              dict(type='assistant', error='rate_limit'), last))

    def test_claude_rate_limit_requires_rejection_and_an_unsuccessful_turn(self):
        last = dict(type='result', subtype='success', is_error=True)
        for status in ('allowed', 'allowed_warning', 'rejected'):
            event = dict(type='rate_limit_event', rate_limit_info={'status': status})
            with self.subTest(status=status):
                actual = self.observe('claude', event, last)
                self.assertEqual(actual, dict(reason='limit', partial=None) if status == 'rejected' else None)
                self.assertIsNone(self.observe('claude', event, dict(last, is_error=False), code=0))

    def test_claude_ignores_errors_in_model_text_or_subagent_and_preserves_last_known_file(self):
        def message(text, **fields):
            return dict(type='assistant', message={'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}, **fields)
        file = message('{"path":"quota.py","content":"API Error: 429"}')
        child = message('{"path":"quota.py","content":"private"}', parent_tool_use_id='child')
        failure = message('{"path":"quota.py","content":"error body"}', error='invalid_request')
        after_error = message('{"path":"quota.py","content":"late"}')
        last = dict(type='result', subtype='success', is_error=True, result='private')
        self.assertIsNone(self.observe('claude', file, child, last))
        self.assertEqual(self.observe('claude', file, child, failure, after_error, last, after_error),
                         dict(reason='api_error', partial=BOM_UTF8 + b'API Error: 429'))

    def test_partial_keeps_newest_valid_parent_message_and_enforces_byte_limit(self):
        first = self.codex_file('{"path":"quota.py","content":"old"}')
        recent = self.codex_file('{"path":"quota.py","content":"kept"}')
        huge = self.codex_file(json.dumps({'path': 'quota.py', 'content': 'x' * (1024 * 1024 + 1)}))
        failed = dict(type='turn.failed', error={'type': 'rate_limit_error'})
        self.assertEqual(self.observe('codex', first, recent, huge, failed),
                         dict(reason='limit', partial=BOM_UTF8 + b'kept'))
        newlines = '\n' * (1024 * 1024)
        at_limit = self.codex_file(json.dumps({'path': 'quota.py', 'content': newlines}))
        self.assertEqual(self.observe('codex', at_limit, failed)['partial'], BOM_UTF8 + b'\r\n' * len(newlines))


class GatewayFailureTests(unittest.TestCase):
    def test_gateway_separates_a_completed_file_from_tools_diagnostics_and_incomplete_messages(self):
        proposed = '{"path":"quota.py","content":"earlier"}'
        file = dict(type='message', role='assistant', status='completed',
                    content=[dict(type='output_text', text=proposed)])
        other = dict(file, content=[dict(type='output_text', text='private-token')])
        ignored = [
            dict(file, status='in_progress'), dict(file, role='user'),
            dict(type='reasoning', summary=[dict(type='summary_text', text=proposed)]),
            dict(type='function_call', name='dummy', arguments=proposed), other,
            dict(file, content=[dict(type='refusal', refusal=proposed)])]
        self.assertIsNone(_file_before_failure(ignored))
        with patch('kit.runtimes.native_responses.MAX_PARTIAL_ENVELOPE', len(proposed) - 1):
            self.assertIsNone(_file_before_failure([file]))
        response = dict(status='failed', error=dict(message='private-token', code='server_error'), output=[file, *ignored])
        client = build_opener(ProxyHandler({}))
        callbacks = []
        with patch('kit.runtimes.native_responses.build_opener') as provider:
            provider.return_value.open.return_value = BytesIO(json.dumps(response).encode())
            with text_responses('http://127.0.0.1:1/v1', None, 'chosen', on_result=callbacks.append,
                                on_partial=callbacks.append) as (address, token):
                call = Request(address + '/responses', data=b'{"input":"text"}', headers={'Authorization': 'Bearer ' + token})
                with self.assertRaises(HTTPError) as raised:
                    client.open(call, timeout=4)
                with raised.exception as rejection:
                    self.assertEqual(rejection.code, 502)
                    public = rejection.read()
                    for private in (proposed, 'private-token', 'dummy'):
                        self.assertNotIn(private.encode(), public)
        self.assertEqual(callbacks, [proposed, 'api_error'])

    def test_gateway_keeps_http_status_and_callback_free_of_error_body(self):
        events = []
        client = build_opener(ProxyHandler({}))
        with patch('kit.runtimes.native_responses.build_opener') as provider:
            provider.return_value.open.side_effect = [
                HTTPError('https://example.invalid', 401, 'private-token', {}, None),
                HTTPError('https://example.invalid', 429, 'private-token', {}, None),
                URLError('private-token'),
                BytesIO(b'{"status":"failed","error":{"code":"server_error","message":"private-token"}}'),
                BytesIO(b'{"status":"failed","error":{"code":"rate_limit_exceeded","message":"private-token"}}')]
            with text_responses('http://127.0.0.1:1/v1', None, 'chosen', on_result=events.append) as (address, token):
                for status in (401, 429, 502, 502, 429):
                    call = Request(address + '/responses', data=b'{"input":"text"}',
                                   headers={'Authorization': 'Bearer ' + token})
                    with self.assertRaises(HTTPError) as raised:
                        client.open(call, timeout=4)
                    with raised.exception as response:
                        self.assertEqual(response.code, status)
                        self.assertNotIn(b'private-token', response.read())
        self.assertEqual(events, ['api_error', 'limit', 'api_error', 'api_error', 'limit'])
