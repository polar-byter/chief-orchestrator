"""Установка закреплённой версии Pi в каталог выбранного проекта."""
from pathlib import Path
import os
import shutil
import subprocess

from .pi_configuration import PI_PACKAGE, PI_VERSION, locate_pi_command
from .task_control_state import plain_path, operation_lock


def npm_command():
    node, npm = shutil.which('node'), shutil.which('npm')
    if not node or not npm:
        return None
    script = Path(npm).resolve()
    if script.name != 'npm-cli.js':
        script = script.parent / 'node_modules/npm/bin/npm-cli.js'
    if not script.is_file():
        return None
    try:
        run = subprocess.run([node, '--version'], capture_output=True, text=True, timeout=6,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        version = tuple(int(part) for part in run.stdout.strip().lstrip('v').split('.')[:3])
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None
    return [node, str(script)] if run.returncode == 0 and version >= (22, 19, 0) else None


def can_install(project):
    return not (os.environ.get('PI_KIT_COMMAND') or (project / '.pi/installation.json').exists()
                or (project / '.tools/pi/node_modules' / PI_PACKAGE / 'package.json').exists())


def ensure_pi(project):
    """Повтор настройки после обрыва использует тот же локальный каталог npm."""
    with operation_lock(project, 'pi-runtime-install'):
        try:
            return locate_pi_command(project)
        except ValueError:
            if not can_install(project):
                raise
        command = npm_command()
        if not command:
            raise ValueError('Для установки Pi нужны Node.js 22.19+ и npm.')
        destination = plain_path(project / '.tools/pi')
        cache = plain_path(project / '.tools/npm-cache')
        run = subprocess.run([*command, 'install', '--prefix', str(destination), '--cache', str(cache),
                              '--ignore-scripts', '--omit=dev', '--no-audit', '--no-fund', '--save-exact', f'{PI_PACKAGE}@{PI_VERSION}'],
                             cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if run.returncode:
            raise ValueError('Не удалось установить Pi из npm. Проверьте подключение и повторите установку.')
        return locate_pi_command(project)
