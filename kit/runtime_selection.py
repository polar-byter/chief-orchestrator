"""Выбор среды проекта. При отсутствии файла настройки используется OpenCode."""
from __future__ import annotations

import json
from pathlib import Path


def uses_pi_runtime(project_root: Path) -> bool:
    """Неизвестная среда требует исправить настройку до запуска исполнителя."""
    selection_path = Path(project_root) / 'pi-kit.json'
    if not selection_path.exists():
        return False
    selection = json.loads(selection_path.read_text(encoding='utf-8-sig'))
    if not isinstance(selection, dict) or selection.get('runtime') not in ('pi', 'opencode'):
        raise ValueError('The project must select the pi or opencode runtime.')
    return selection['runtime'] == 'pi'


def create_session_gateway(project_root: Path):
    """Создание шлюза не открывает сессию и не запускает модель."""
    if uses_pi_runtime(project_root):
        from .runtimes.pi_gateway import PiSessionControl
        return PiSessionControl(project_root)
    from .dashboard.session_control import SessionControl
    return SessionControl(project_root)


def check_task_runtime(project_root: Path, start_event: dict) -> None:
    """Идентификатор сессии имеет смысл только в среде, где была начата задача."""
    current = 'pi' if uses_pi_runtime(project_root) else 'opencode'
    recorded = start_event.get('runtime', 'opencode')
    if recorded != current:
        raise ValueError(f'Задача начата в среде {recorded}. Сейчас для проекта выбрана {current}.')


def load_project_model_choices(project_root: Path) -> dict:
    """Читает модели из настроек выбранной среды."""
    if uses_pi_runtime(project_root):
        from .pi_configuration import load_choices
    else:
        from .run_opencode import load_choices
    return load_choices(project_root)
