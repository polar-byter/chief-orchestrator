"""Установка и обновление общего скилла в каталоге выбранного агента проекта."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from .installer import MANIFEST
from .project_vault import publish_new
from .task_control_state import operation_lock, plain_path
from .chief_mode import managed_path


AGENT_FOLDERS = {'codex': '.agents', 'pi': '.agents', 'opencode': '.agents', 'claude': '.claude'}
SKILL_FILE = Path('skills/chief-orchestrator/SKILL.md')
SOURCE = Path(__file__).parent / SKILL_FILE
# Отпечатки поставленных редакций позволяют обновлять неизменённые копии навыка.
LEGACY_HASHES = frozenset((
    'd59b8c2f43790cf81733f1e1ea47f47ad7761c8d7c91d92710c04a3d66999c38',
    '9608008d45b6db76b4028680a5b78bf59ef5b5f5dd280efab7cb8057b79180da',
    'd4cdc91c03960525fbc7fefe9e3c368300dbf8ae09592d7e5a5197ed2869ed51',
    '4c0d2cd1edcc582521fa37d897d2d97f86a25680441f2f17278934758a246860',
    '3526d9c5e4f5586337e86933eb61956f78b377c095673faeb136fd434da856a7',
    'ad133ec6799b1fd33f9db7298ca438313602804ba2777583c9c6338d4d6e3c56',
    '344252a2e81d6cb4caec41f7e8d3271b5332bc347b3c56927311d6f08d17038f',
))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _owned_hashes(root, relative, marker):
    # Первичный установщик учитывал исходник комплекта, но ещё не его копии для агентов.
    sources = [(plain_path(root / MANIFEST), (Path('kit') / SKILL_FILE).as_posix()),
               (marker, relative.as_posix())]
    known = set(LEGACY_HASHES)
    for path, key in sources:
        if not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            value = record['files'][key] if record['format'] == 1 else None
        except (KeyError, ValueError, TypeError):
            continue
        if isinstance(value, str):
            known.add(value)
    return known


def _replace_if_unchanged(target, original, content):
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, prefix='.chief-skill-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if plain_path(target).read_bytes() != original:
            raise ValueError(f'Файл изменён во время обновления. Повторите установку: {target}')
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _offer_copy(target, content):
    candidate = plain_path(target.with_name('SKILL.md.new'))
    if not publish_new(candidate, content) and candidate.read_bytes() != content:
        candidate = plain_path(target.with_name('SKILL.md.' + _digest(content) + '.new'))
        if not publish_new(candidate, content) and candidate.read_bytes() != content:
            raise ValueError(f'Имена копий поставки уже заняты. Сверьте {target} с {SOURCE}.')
    raise ValueError(f'Сохранена ваша инструкция {target}. Новая поставка для сравнения: {candidate}')


def install_chief_skill(project: Path, agent: str) -> Path:
    """Обновляет известные файлы комплекта. При расхождении оставляет обе версии."""
    if agent not in AGENT_FOLDERS:
        raise ValueError(f'Неизвестная среда: {agent}')
    root = plain_path(project.absolute())
    if not plain_path(root / 'kit/prompts/master-chief.md').is_file():
        raise ValueError('В проекте отсутствует kit/prompts/master-chief.md. Сначала установите комплект.')
    folder = AGENT_FOLDERS[agent]
    relative = Path(folder) / SKILL_FILE
    marker = plain_path(root / '.metrics' / ('chief-skill-' + folder[1:] + '.json'))
    content = plain_path(SOURCE).read_bytes()
    digest = _digest(content)
    stamp = (json.dumps(dict(format=1, files={relative.as_posix(): digest}), indent=2) + '\n').encode()
    with operation_lock(root, 'chief-mode-switch'), operation_lock(root, 'chief-skill-install-' + folder[1:]):
        target = managed_path(root, relative.as_posix())
        # Для первого запуска метки нет. После прерывания между двумя записями
        # совпавшие байты SKILL.md позволяют довести до конца только запись метки.
        if not target.exists():
            publish_new(target, content)
        original = plain_path(target).read_bytes()
        if original != content:
            if _digest(original) not in _owned_hashes(root, relative, marker):
                _offer_copy(target, content)
            _replace_if_unchanged(target, original, content)
        if not marker.exists():
            publish_new(marker, stamp)
        previous = plain_path(marker).read_bytes()
        if previous != stamp:
            try:
                record = json.loads(previous)
                valid = record['format'] == 1 and set(record['files']) == {relative.as_posix()}
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                raise ValueError(f'Имя служебной метки занято. Файл сохранён: {marker}')
            _replace_if_unchanged(marker, previous, stamp)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=AGENT_FOLDERS)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    options = parser.parse_args(argv)
    try:
        print(install_chief_skill(options.root, options.agent))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
