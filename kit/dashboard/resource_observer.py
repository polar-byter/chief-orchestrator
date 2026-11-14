"""Сохраняет изменения оценок подписанных этапов, пока работает дашборд."""
from pathlib import Path
import sqlite3
import sys
import threading

from ..project_vault import BINDING_FILE, ProjectVault
from ..stage_decisions import resource_forecast
from ..stage_resource_ledger import observe_forecast, tracked_stages
from ..task_control_state import plain_path


def _stamp(path):
    try:
        stat = plain_path(path).stat()
        return stat.st_mtime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return None


def _input_signature(vault, members):
    documents = vault.path / 'Документы'
    return (vault.project_id, vault.path,
            tuple((key, _stamp(documents / key / 'Оценка трудозатрат.md'),
                   _stamp(documents / key / 'Риски.md')) for key in members))


class ResourceObserver:
    def __init__(self, root: Path, navigation=None):
        self.root = root
        self.navigation = navigation
        self.signatures = {}
        self.stop_requested = threading.Event()
        self.thread = None
        self.errors = {}

    def start(self):
        self.thread = threading.Thread(target=self._run, name='stage-resource-history', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_requested.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _error(self, key, error):
        if self.errors.get(key) != str(error):
            print(f'История ресурсов, {key}: {error}', file=sys.stderr, flush=True)
            self.errors[key] = str(error)

    def poll(self):
        if not plain_path(self.root / BINDING_FILE).is_file():
            self.signatures.clear()
            return
        vault = ProjectVault.load(self.root)
        records = vault.records()
        if self.navigation:
            try:
                self.navigation.refresh(vault, records)
                self.errors.pop('переход в дашборд', None)
            except (OSError, ValueError, sqlite3.Error) as error:
                self._error('переход в дашборд', error)
        subscribed = tracked_stages(vault)
        if not subscribed:
            self.signatures.clear()
            return
        for stage in subscribed:
            # Состав каталога и две таблицы влияют на расчёт. Открытие страницы и правка BDD — нет.
            members = [r['key'] for r in records if r['key'] == stage or r['parent'] == stage]
            try:
                signature = _input_signature(vault, members)
                if self.signatures.get(stage) != signature:
                    observe_forecast(vault, stage, resource_forecast)
                    self.signatures[stage] = signature
                self.errors.pop(stage, None)
            except (OSError, ValueError, sqlite3.Error) as error:
                self._error(stage, error)

    def _run(self):
        while not self.stop_requested.wait(2):
            try:
                self.poll()
                self.errors.pop('база проекта', None)
            except (OSError, ValueError, sqlite3.Error) as error:
                self._error('база проекта', error)
