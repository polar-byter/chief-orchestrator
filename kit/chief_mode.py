"""Проектный режим Чифа и обратимый обмен его инструкций с обычным профилем."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import sys
from uuid import UUID

from .project_vault import BINDING_FILE, PROJECT_FILE, _publish, publish_new
from .task_control_state import operation_lock, plain_path


DIRECTORY = Path('.metrics/.chief-mode')
STATE = DIRECTORY / 'state.json'
MANIFEST = Path('.metrics/.kit-installation.json')
GENERATED = '.opencode/opencode.jsonc'
# OpenCode объединяет настройки из нескольких известных имён. При включении
# временно освобождаем их для конфигурации поставки и её генерируемого overlay.
OPENCODE_ALTERNATES = ('opencode.json', '.opencode/opencode.json', GENERATED)
SKILLS = ('.agents/skills/chief-orchestrator/SKILL.md', '.claude/skills/chief-orchestrator/SKILL.md')


def activation_file(name):
    if not isinstance(name, str) or '\\' in name or ':' in name:
        return False
    parts = PurePosixPath(name).parts
    return (name in ('AGENTS.md', 'opencode.jsonc', *OPENCODE_ALTERNATES, *SKILLS)
            or len(parts) >= 3 and parts[:2] == ('.opencode', 'commands') and '..' not in parts
            and '/'.join(parts) == name and name.endswith('.md'))


def _data(path):
    try:
        return json.loads(plain_path(path).read_bytes())
    except FileNotFoundError:
        return None


def _installed(root):
    from .installer import installed
    return installed(root) or _checkout(root) or _bound_project(root)


def _bound_project(root):
    """Привязка прежней версии подтверждает использование Чифа, но не владение AGENTS.md."""
    project, binding = _data(root / PROJECT_FILE), _data(root / BINDING_FILE)
    if not isinstance(project, dict) or not isinstance(binding, dict):
        return False
    identity = project.get('id')
    try:
        return (project.get('format') == binding.get('format') == 1 and isinstance(identity, str)
                and str(UUID(identity)) == binding.get('id') == identity
                and isinstance(binding.get('path'), str) and Path(binding['path']).is_absolute())
    except ValueError:
        return False


def _checkout(root):
    # У исходной поставки нет манифеста установки, но есть весь набор публичных точек входа.
    return plain_path(root / '.git').exists() and root == Path(__file__).resolve().parents[1] and all(
        plain_path(root / name).is_file() for name in
        ('kit/installer.py', 'kit/project_docs.py', 'kit/prompts/master-chief.md', 'install.py'))


def mode_state(root):
    root = plain_path(Path(root).absolute())
    state = _data(root / STATE)
    if state is None:
        enabled = _installed(root)
        return dict(enabled=enabled, explicit=False, status='on' if enabled else 'off')
    if (not isinstance(state, dict) or state.get('format') != 1
            or state.get('status') not in ('on', 'off', 'enabling', 'disabling')):
        raise ValueError('Неизвестное состояние режима Чифа. Проверьте ' + str(root / STATE))
    return dict(enabled=state['status'] == 'on', explicit=True, status=state['status'])


def require_enabled(root):
    if not mode_state(root)['enabled']:
        raise ValueError('Режим Чифа выключен или переключается. Для запуска выполните kit.chief_mode on.')


@contextmanager
def gateway_admission(root):
    """Сериализует допуск старого шлюза со сменой режима установленного проекта."""
    with operation_lock(root, 'chief-mode-switch'):
        if mode_state(root)['explicit']:
            require_enabled(root)
        yield


def staged_path(root, name):
    return plain_path(Path(root) / DIRECTORY / 'chief' / name)


def managed_path(root, name):
    """Установщик и обновление записывают инструкции туда, откуда их прочтёт следующий on."""
    if activation_file(name):
        state = mode_state(root)
        if state['status'] in ('enabling', 'disabling'):
            raise ValueError('Сначала завершите переключение режима Чифа.')
        if state['explicit'] and not state['enabled']:
            return staged_path(root, name)
    return plain_path(Path(root) / name)


def _save(root, state):
    _publish(root / STATE, encoded_state(state), replace=True)


def encoded_state(state):
    return (json.dumps(dict(state, format=1), ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _hash(path):
    path = plain_path(path)
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _files(root, previous):
    names = set(previous.get('files', []))
    if not all(activation_file(name) for name in names):
        raise ValueError('Недопустимое имя управляемой инструкции в журнале режима.')
    manifest = _data(root / MANIFEST)
    if manifest:
        if manifest.get('format') != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('Неизвестный манифест установки.')
        names.update(name for name in manifest['files'] if activation_file(name))
    elif _checkout(root):
        names.update(('AGENTS.md', 'opencode.jsonc'))
        # Это команды исходной поставки. Соседние команды вправе создавать владелец проекта.
        names.update('.opencode/commands/' + filename for filename in ('module.md', 'plan-project.md'))
    if previous.get('status') == 'off' and (manifest or _checkout(root)):
        names.update(name for name in OPENCODE_ALTERNATES if plain_path(root / name).is_file())
    elif previous.get('status') != 'off':
        overlay = plain_path(root / GENERATED)
        if overlay.is_file():
            from .run_opencode import HEADER
            with overlay.open(encoding='utf-8-sig', errors='replace') as stream:
                owned = stream.readline() == HEADER
            if owned:
                names.add(GENERATED)
    for skill in SKILLS:
        from .install_chief_skill import LEGACY_HASHES, SKILL_FILE
        marker = _data(root / '.metrics' / ('chief-skill-' + skill.split('/')[0][1:] + '.json'))
        if marker and skill in marker.get('files', {}):
            names.add(skill)
        elif previous.get('status') != 'off':
            digest = _hash(root / skill)
            source = _hash(root / 'kit' / SKILL_FILE)
            if digest and (digest == source or digest in LEGACY_HASHES):
                names.add(skill)
    return sorted(names)


def _move(source, target, digest):
    """Сбой между публикацией и освобождением имени оставляет две проверяемые копии."""
    if digest is None:
        return
    source, target = plain_path(source), plain_path(target)
    current = _hash(source)
    other = _hash(target)
    if other is None:
        if current != digest:
            raise ValueError(f'Файл изменился во время переключения: {source}')
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f'Файл изменился во время чтения: {source}')
        publish_new(target, content)
        other = _hash(target)
    if other != digest or current not in (None, digest):
        raise ValueError(f'Проверьте обе версии инструкции: {source} и {target}')
    if current is not None:
        if _hash(source) != digest:
            raise ValueError(f'Файл изменился во время переключения: {source}')
        source.unlink()


def _switch(root, state):
    enabling = state['status'] == 'enabling'
    for name, step in state['moves'].items():
        if not activation_file(name):
            raise ValueError('Неизвестный путь в журнале переключения.')
        live = plain_path(root / name)
        parked = staged_path(root, name)
        ordinary = plain_path(root / DIRECTORY / 'ordinary' / name)
        source, target = (parked, ordinary) if enabling else (ordinary, parked)
        if step['phase'] == 'save':
            _move(live, target, step['active'])
            step['phase'] = 'restore'
            _save(root, state)
        if step['phase'] == 'restore':
            if step['restore'] is None and live.exists():
                raise ValueError(f'При переключении появилось новое содержимое: {live}')
            _move(source, live, step['restore'])
            step['phase'] = 'done'
            _save(root, state)


def set_mode(root, enabled: bool):
    root = plain_path(Path(root).absolute())
    if not _installed(root):
        if not enabled:
            return mode_state(root)
        raise ValueError('Сначала установите комплект Чифа в проект.')
    desired, transition = ('on', 'enabling') if enabled else ('off', 'disabling')
    with operation_lock(root, 'chief-mode-switch'):
        state = _data(root / STATE) or dict(status='on', files=[])
        mode_state(root)
        if state['status'] == desired:
            return mode_state(root)
        if plain_path(root / '.metrics/.kit-update.json').exists():
            raise ValueError('Завершите прерванное обновление поставки до переключения режима.')
        if state['status'] not in ('on', 'off', transition):
            raise ValueError('Сначала завершите предыдущее переключение режима.')
        if state['status'] != transition:
            names = _files(root, state)
            state = dict(status=transition, files=names)
            # Блокируем новый приём до обращения к контроллеру и перед подготовкой снимков.
            _save(root, state)
        if not enabled:
            from .native_team import pause_project
            pause_project(root)
        if 'moves' not in state:
            moves = {}
            for name in state['files']:
                incoming = staged_path(root, name) if enabled else plain_path(root / DIRECTORY / 'ordinary' / name)
                destination = plain_path(root / DIRECTORY / 'ordinary' / name) if enabled else staged_path(root, name)
                if not incoming.exists() and not plain_path(root / name).exists():
                    # Удалённый обеими сторонами вход не требует обмена.
                    continue
                if destination.exists():
                    raise ValueError(f'Путь сохранения инструкции уже занят: {destination}')
                moves[name] = dict(active=_hash(root / name), restore=_hash(incoming), phase='save')
            state['moves'] = moves
            state['files'] = list(moves)
            _save(root, state)
        _switch(root, state)
        _save(root, dict(status=desired, files=state['files']))
        return mode_state(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('on', 'off', 'status'))
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        result = mode_state(args.root) if args.action == 'status' else set_mode(args.root, args.action == 'on')
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
