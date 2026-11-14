"""Из потока одного хода извлекаем причину отказа провайдера и доступный файл для сверки."""
import json
import re

from . import native_worker


QUOTA_TYPES = frozenset(('rate_limit', 'rate_limit_error', 'rate_limit_exceeded', 'usage_limit_reached',
                         'insufficient_quota', 'provider.quota'))
API_TYPES = frozenset(('authentication_failed', 'authentication_error', 'billing_error', 'invalid_request',
                       'invalid_request_error', 'permission_error', 'not_found_error', 'server_error', 'api_error',
                       'overloaded_error', 'api_connection_error', 'api_timeout_error'))
# Позиция статуса существенна: в остальном тексте могут встретиться номер строки и тело ответа.
HTTP_STATUS = re.compile(r'(?:unexpected status|last status:|http(?: error)?[:/]?|api error:)\s+([45]\d{2})\b', re.I)
NETWORK_FAILURE = re.compile(r'^(?:error sending request for url|failed to connect to (?:the )?(?:api|server)|'
                             r'stream disconnected before completion)\b', re.I)
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}\Z')


def _provider_reason(error):
    if isinstance(error, dict):
        for name in ('status', 'status_code', 'statusCode', 'code'):
            status = error.get(name)
            if type(status) is int and 400 <= status <= 599:
                return 'limit' if status == 429 else 'api_error'
        for name in ('type', 'code', 'name'):
            kind = error.get(name)
            if isinstance(kind, str):
                if kind in QUOTA_TYPES:
                    return 'limit'
                if kind in API_TYPES:
                    return 'api_error'
        error = error.get('message')
    if isinstance(error, str):
        if error in QUOTA_TYPES:
            return 'limit'
        if error in API_TYPES:
            return 'api_error'
        status = HTTP_STATUS.search(error[:500])
        if status:
            return 'limit' if status[1] == '429' else 'api_error'
        if NETWORK_FAILURE.match(error):
            return 'api_error'
    return None


def _candidate(plan, text):
    if not plan.get('assigned') or not isinstance(text, str):
        return None
    try:
        value = json.loads(text, object_pairs_hook=native_worker._unique_keys)
        # Размер текста проверяется до восстановления исходных CRLF и BOM.
        return native_worker.candidate_bytes(plan, {'candidate': value})
    except (ValueError, RecursionError):
        return None


def _events(stdout):
    for line in stdout.splitlines():
        if line.strip():
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                event = None
            yield event if isinstance(event, dict) else {}


def _codex(plan, stdout, returncode):
    signal = gateway = partial = gateway_text = None
    terminal = ''
    for event in _events(stdout):
        kind = event.get('type')
        # Хост пишет квитанцию после выхода CLI; запись в середине потока не является итоговой.
        gateway = gateway_text = None
        if kind == 'chief.gateway_result':
            gateway = event.get('outcome')
            gateway_text = event.get('partial_text')
        elif kind == 'turn.started':
            signal = partial = None
            terminal = ''
        elif kind == 'turn.completed':
            terminal = 'completed'
        elif kind == 'turn.failed':
            terminal = 'failed'
            signal = _provider_reason(event.get('error')) or signal
        elif not terminal:
            if kind == 'error':
                signal = _provider_reason(event.get('error') or event.get('message'))
            elif kind == 'item.completed' and not signal:
                item = event.get('item')
                if isinstance(item, dict) and item.get('type') == 'agent_message':
                    content = _candidate(plan, item.get('text'))
                    if content is not None:
                        partial = content
    if terminal == 'completed' or not terminal and returncode == 0:
        return None
    if gateway in ('ok', 'invalid_response'):
        return None
    if gateway in ('limit', 'api_error'):
        content = _candidate(plan, gateway_text)
        return dict(reason=gateway, partial=partial if content is None else content)
    return dict(reason=signal, partial=partial) if signal else None


def _claude(plan, stdout, returncode):
    reason = model = partial = None
    terminal = ''
    for event in _events(stdout):
        if event.get('parent_tool_use_id') or event.get('agent_id'):
            continue
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            value = event.get('model')
            if isinstance(value, str) and MODEL_NAME.fullmatch(value):
                model = value
        elif kind == 'result':
            if event.get('subtype') not in ('success', 'error_during_execution') or event.get('is_error') is not True:
                return None
            terminal = 'failed'
        elif not terminal and kind == 'rate_limit_event':
            info = event.get('rate_limit_info')
            if isinstance(info, dict) and info.get('status') == 'rejected':
                reason = 'limit'
        elif not terminal and kind == 'assistant':
            if event.get('error'):
                reason = _provider_reason(event['error']) or reason
                continue
            message = event.get('message')
            if not reason and isinstance(message, dict) and message.get('role') == 'assistant':
                blocks = message.get('content')
                if isinstance(blocks, list):
                    parts = [b['text'] for b in blocks if isinstance(b, dict) and b.get('type') == 'text'
                             and isinstance(b.get('text'), str)]
                    content = _candidate(plan, ''.join(parts))
                    if content is not None:
                        partial = content
    if not reason or not terminal and returncode == 0:
        return None
    result = dict(reason=reason, partial=partial)
    if model:
        result['model'] = model
    return result


def observe(plan, stdout, returncode):
    """Отсутствие признаков отказа оставляет решение обычному разбору результата CLI."""
    if not isinstance(stdout, (bytes, str)) or len(stdout) > 16 * native_worker.MAX_FILE:
        return None
    if plan['agent'] == 'codex':
        return _codex(plan, stdout, returncode)
    if plan['agent'] == 'claude':
        return _claude(plan, stdout, returncode)
    return None
