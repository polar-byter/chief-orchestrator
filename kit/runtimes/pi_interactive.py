"""Управление сессией в открытом терминале Pi."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from urllib.request import Request

from .pi_rpc import PiControlError, process_identity


class InteractiveProcessReference:
    """Сверяет владельца PID так же, как при управлении дочерним процессом."""

    def __init__(self, identifier, identity):
        self.pid = identifier
        self.identity = identity
        self.returncode = None

    def poll(self):
        if process_identity(self.pid) != self.identity:
            self.returncode = 1
        return self.returncode


class PiInteractiveProcess:
    """Посылает команды одной сессии по защищённому каналу её терминала."""

    def __init__(self, session):
        endpoint = session['interactive']
        self.session_identifier = session['id']
        self.identity = session['process']['identity']
        self.process = InteractiveProcessReference(session['process']['pid'], self.identity)
        self.port = endpoint['port']
        self.token = endpoint['token']
        self.settled = threading.Event()
        self.settled.set()

    def request(self, operation, timeout=20, **fields):
        from ..dashboard.launcher import local_request
        if self.process.poll() is not None:
            raise PiControlError('The native Pi terminal has exited')
        request = Request('http://127.0.0.1:' + str(self.port) + '/call',
            json.dumps(dict(session=self.session_identifier, operation=operation, **fields)).encode('utf-8'),
            {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with local_request(request, timeout=timeout) as response:
                result = json.load(response)
            if not result.get('success'):
                raise PiControlError('Native Pi did not confirm operation: ' + operation)
            data = result.get('data')
            if operation == 'get_state':
                if data.get('settled'):
                    self.settled.set()
                else:
                    self.settled.clear()
            elif operation == 'prompt':
                self.settled.clear()
            return data
        except (OSError, ValueError, KeyError) as error:
            raise PiControlError('Native Pi control was not confirmed; inspect its terminal') from error

    def stop_run(self):
        self.request('clear_queue')
        self.request('abort')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.request('get_state')
            if (state.get('settled') and not state.get('isStreaming')
                    and not state.get('isCompacting') and not state.get('pendingMessageCount')):
                return
            time.sleep(.05)
        raise PiControlError('Native Pi has not confirmed interruption')

    def close(self):
        """Перезапуск службы останавливает работу и сохраняет терминал пользователя."""
        if self.process.poll() is None:
            self.stop_run()

    def shutdown(self):
        """Явное удаление сессии закрывает её терминал."""
        self.stop_run()
        self.request('shutdown')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return
            time.sleep(.05)
        raise PiControlError('Native Pi has not confirmed shutdown')
