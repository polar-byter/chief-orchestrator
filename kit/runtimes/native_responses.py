"""Граница Codex Responses: исполнителю передаётся только текст ответа модели."""
from __future__ import annotations

from contextlib import contextmanager
import hmac
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BODY = 12 * 1024 * 1024
MAX_PARTIAL_ENVELOPE = 7 * 1024 * 1024  # Исходник до 1 МиБ, включая возможное JSON-экранирование.


def endpoint(value):
    """Незашифрованный транспорт допустим для модели на этом компьютере."""
    url = urlsplit(value)
    if (url.username or url.password or url.query or url.fragment or not url.hostname
            or not (url.scheme == 'https' or url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1'))):
        raise ValueError('Укажите HTTPS-адрес провайдера или HTTP-адрес loopback.')
    return value.rstrip('/')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def text_response(value):
    """Сначала проверяет весь ответ. Незавершённый ответ или вызов инструмента до CLI не доходит."""
    if (not isinstance(value, dict) or value.get('status') != 'completed'
            or not isinstance(value.get('id'), str) or not isinstance(value.get('output'), list)):
        raise ValueError('Модель не подтвердила завершение текстового ответа.')
    messages = []
    for item in value['output']:
        if item.get('type') == 'reasoning':
            continue
        if item.get('type') != 'message' or item.get('role') != 'assistant':
            raise ValueError('Инструменты исполнителя отключены. Модель вернула нетекстовое действие.')
        for block in item.get('content', []):
            if block.get('type') != 'output_text' or not isinstance(block.get('text'), str):
                raise ValueError('Для этого хода нужен текст без вызовов инструментов.')
            messages.append(block['text'])
    if not messages:
        raise ValueError('Модель вернула пустой результат.')
    return ''.join(messages)


def _file_before_failure(output):
    """В ответе с отказом пригоден лишь завершённый текст, в котором умещается отдельное предложение файла."""
    if not isinstance(output, list):
        return None
    for item in reversed(output):
        if (not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'assistant'
                or item.get('status') != 'completed'):
            continue
        blocks = item.get('content')
        if not isinstance(blocks, list) or not blocks or not all(isinstance(block, dict) and
                block.get('type') == 'output_text' and isinstance(block.get('text'), str) for block in blocks):
            continue
        text = ''.join(block['text'] for block in blocks)
        if len(text) > MAX_PARTIAL_ENVELOPE:
            continue
        try:
            if len(text.encode()) > MAX_PARTIAL_ENVELOPE:
                continue
            file = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if (isinstance(file, dict) and set(file) == {'path', 'content'}
                and isinstance(file['path'], str) and isinstance(file['content'], str)):
            return text
    return None


def _events(value):
    text = text_response(value)
    # Создаём кадры только из проверенной строки. Произвольные поля провайдера
    # (включая tool calls в потоке) не пересылаются нативному клиенту.
    msg = dict(type='message', id='msg_' + secrets.token_hex(12), role='assistant',
               status='completed', content=[dict(type='output_text', text=text, annotations=[])])
    response = dict(id=value['id'], object='response', model=value.get('model'), status='completed',
                    created_at=value.get('created_at', 0), output=[msg])
    usage = value.get('usage')
    if isinstance(usage, dict):
        response['usage'] = usage
    added = dict(msg, content=[], status='in_progress')
    frames = [
        ('response.created', dict(response=dict(response, output=[], status='in_progress'))),
        ('response.output_item.added', dict(output_index=0, item=added)),
        ('response.content_part.added', dict(item_id=msg['id'], output_index=0, content_index=0,
                                             part=dict(type='output_text', text='', annotations=[]))),
        ('response.output_text.delta', dict(item_id=msg['id'], output_index=0, content_index=0, delta=text)),
        ('response.output_text.done', dict(item_id=msg['id'], output_index=0, content_index=0, text=text)),
        ('response.content_part.done', dict(item_id=msg['id'], output_index=0, content_index=0, part=msg['content'][0])),
        ('response.output_item.done', dict(output_index=0, item=msg)),
        ('response.completed', dict(response=response))]
    return ''.join('event: ' + name + '\ndata: ' + json.dumps(dict(type=name, sequence_number=i, **fields)) + '\n\n'
                   for i, (name, fields) in enumerate(frames)).encode()


@contextmanager
def text_responses(base_url, api_key, model, *, on_result=None, on_partial=None):
    target = endpoint(base_url) + '/responses'
    password = secrets.token_hex(32)
    opener = build_opener(NoRedirect(), ProxyHandler({}) if urlsplit(target).scheme == 'http' else ProxyHandler())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Ни заголовки доступа, ни фрагменты рабочего файла в журнал не попадают.

        def reject(self, status, outcome, message):
            if on_result is not None:
                on_result(outcome)
            category = 'rate_limit_error' if outcome == 'limit' else 'api_error'
            rejection = json.dumps({'error': {'type': category, 'message': message}}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(rejection)))
            self.end_headers()
            try:
                self.wfile.write(rejection)
            except ConnectionError:
                pass  # Клиент успел завершить ход.

        def do_POST(self):
            if self.path != '/v1/responses' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + password):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    raise ValueError('Некорректный размер запроса.')
                request = json.loads(self.rfile.read(size))
                # Один ход целиком в stdin. Источники не дополняются историей или файлами другого диалога.
                payload = {key: request[key] for key in ('input', 'instructions', 'text', 'reasoning') if key in request}
                payload.update(model=model, tools=[], tool_choice='none', stream=False, store=False)
                headers = {'Content-Type':'application/json'}
                if api_key:
                    headers['Authorization'] = 'Bearer ' + api_key
                upstream = Request(target, data=json.dumps(payload).encode(), headers=headers)
            except (TypeError, ValueError, KeyError):
                self.send_error(400, 'Invalid text request')
                return
            try:
                with opener.open(upstream, timeout=180) as result:
                    raw = result.read(MAX_BODY + 1)
            except HTTPError as error:
                error.close()
                # Код — факт ответа провайдера. Тело и заголовки могут содержать приватные данные.
                status = error.code if 400 <= error.code <= 599 else 502
                self.reject(status, 'limit' if status == 429 else 'api_error', f'Provider HTTP {status}')
                return
            except (OSError, HTTPException):
                self.reject(502, 'api_error', 'Provider connection failed')
                return
            try:
                if len(raw) > MAX_BODY:
                    raise ValueError('Ответ модели слишком велик.')
                value = json.loads(raw)
                if isinstance(value, dict) and value.get('status') == 'failed' and isinstance(value.get('error'), dict):
                    # Responses может вернуть HTTP 200 с отказом внутри объекта response.
                    if on_partial is not None:
                        partial = _file_before_failure(value.get('output'))
                        if partial is not None:
                            on_partial(partial)
                    code = value['error'].get('code')
                    if code in ('rate_limit_exceeded', 'rate_limit_error', 'insufficient_quota'):
                        self.reject(429, 'limit', 'Provider rate limit reached')
                    else:
                        self.reject(502, 'api_error', 'Provider response failed')
                    return
                body = _events(value)
            except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
                self.reject(502, 'invalid_response', 'A completed text-only model response is required')
                return
            if on_result is not None:
                on_result('ok')
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                pass  # Контроллер уже остановил CLI; печатать его старый ответ некому.

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval':.1}, daemon=True)
        thread.start()
        try:
            yield 'http://127.0.0.1:' + str(server.server_port) + '/v1', password
        finally:
            server.shutdown()
            thread.join()
