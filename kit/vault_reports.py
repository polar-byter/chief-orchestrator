"""Публикация вычисляемых заметок. Исходники пользователя остаются у него."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .project_vault import _publish, publish_new
from .task_control_state import plain_path


def append_note(path: Path, addition: str) -> None:
    if addition:
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(plain_path(path), flags), 'wb') as output:
            output.write(addition.encode('utf-8'))


def publish_report(path: Path, identity: str, text: str) -> None:
    """Заменяет только отчёт того же владельца с неизменённым содержимым."""
    path = plain_path(path)
    prefix = f'---\nchief_report: {identity}\nchecksum: '.encode()
    separator = b'\n---\n\n'
    body = text.encode('utf-8')
    generated = prefix + hashlib.sha256(body).hexdigest().encode() + separator + body
    if not path.exists() and publish_new(path, generated):
        return
    old = path.read_bytes()
    if old == generated:
        return
    head, found, old_body = old.partition(separator)
    if not found or head != prefix + hashlib.sha256(old_body).hexdigest().encode():
        raise ValueError(f'В файле результата есть другой текст. Сохраните его под другим именем: {path}')
    _publish(path, generated, replace=True)
