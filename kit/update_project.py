"""Планирование и применение новой поставки Чифа в ранее установленном проекте."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import uuid4

from .installer import InstallError, MANIFEST, PI_MANIFEST, project_agent, project_path, project_payload
from .project_vault import publish_new
from .task_control_state import operation_lock, plain_path
from .chief_mode import managed_path


JOURNAL = MANIFEST.with_name('.kit-update.json')
# Действующая базовая конфигурация может определять агента и модель ещё до появления локального overlay.
OPERATOR_FILES = frozenset({'opencode.jsonc'})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _hashes(value) -> dict:
    if not isinstance(value, dict) or not all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v)
                                               for v in value.values()):
        raise InstallError('Некорректный список SHA-256 в манифесте обновления.')
    return value


def _file_hash(path: Path) -> str | None:
    try:
        mode = plain_path(path).stat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise InstallError(f'Ожидался обычный файл: {path}')
    return _digest(path.read_bytes())


@dataclass
class Plan:
    manifest: dict
    manifest_hash: str
    target_hashes: dict
    report: dict

    @property
    def changed(self):
        return {name: self.target_hashes[name] for name in (*self.report['update'], *self.report['add'])}


def _plan(project, contents, manifest_path=MANIFEST):
    path = plain_path(project / manifest_path)
    if not path.is_file():
        raise InstallError('Манифест установки отсутствует. Для нового проекта используйте установщик.')
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if not isinstance(manifest, dict) or manifest.get('format') != 1 or not manifest.get('files'):
        raise InstallError('Манифест установки отсутствует или имеет неизвестный формат.')
    baseline = _hashes(manifest['files'])
    if manifest_path == PI_MANIFEST:
        if (manifest.get('runtime') != 'pi' or not {'pi-kit.json', 'AGENTS.md', 'kit/run_pi.py'} <= baseline.keys()
                or project_agent(project) != 'pi'):
            raise InstallError('Манифест старой установки Pi расходится с настройками проекта.')
    hashes = {name: _digest(content) for name, content in contents.items()}
    report = dict(update=[], add=[], conflicts=[], preserved_config=[], retired=[], unchanged=0)
    # Пути берём только из белого списка текущей поставки. Имена из старого манифеста не служат адресами записи.
    for name, desired in sorted(hashes.items()):
        if name in OPERATOR_FILES:
            if name in baseline or managed_path(project, name).exists():
                report['preserved_config'].append(name)
                continue
        prior = baseline.get(name)
        try:
            current = _file_hash(managed_path(project, name))
        except (OSError, ValueError) as error:
            report['conflicts'].append(dict(path=name, reason=str(error)))
            continue
        if current == prior:
            if current == desired:
                report['unchanged'] += 1
            else:
                report['add' if prior is None else 'update'].append(name)
        elif prior is not None and current == desired:
            # Локальная правка могла уже внести ровно те байты, которые вошли в новую поставку.
            report['update'].append(name)
        else:
            reason = ('Локальный файл уже занимает новое имя.' if prior is None else
                      'Файл удалён в проекте.' if current is None else 'Содержимое изменено в проекте.')
            report['conflicts'].append(dict(path=name, reason=reason))
    report['retired'] = sorted(baseline.keys() - hashes.keys())
    return Plan(manifest, _digest(raw), hashes, report)


def _transaction(plan, identifier):
    owned = plan.manifest['files'] | plan.changed
    updated = dict(plan.manifest, files=owned)
    final = _json(updated)
    return dict(format=1, id=identifier, before=plan.manifest_hash, after=_digest(final),
                files=plan.changed)


def _replace(path, data, expected, work, identifier):
    """Пишет заготовку и атомарно меняет имя после повторной сверки рабочей версии."""
    temporary = plain_path(work / (identifier + '.tmp'))
    temporary.unlink(missing_ok=True)
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    mode = plain_path(path).stat().st_mode
    os.chmod(temporary, stat.S_IMODE(mode))
    if _file_hash(path) != expected:
        raise InstallError(f'Файл изменился во время обновления: {path}')
    os.replace(temporary, path)


def _finish(project, journal, old_manifest, contents):
    """Журнал переживает остановку между любыми двумя публикациями, включая запись манифеста."""
    if not isinstance(journal, dict):
        raise InstallError('Повреждён журнал незавершённого обновления.')
    identifier = journal.get('id')
    if (journal.get('format') != 1 or not isinstance(identifier, str) or not re.fullmatch('[0-9a-f]{32}', identifier)
            or not isinstance(journal.get('files'), dict) or not journal['files']):
        raise InstallError('Повреждён журнал незавершённого обновления.')
    changes = _hashes(journal['files'])
    path = plain_path(project / MANIFEST)
    work = plain_path(project / JOURNAL).parent
    actual = _file_hash(path)
    if actual != journal.get('after'):
        if any(name not in contents or name in OPERATOR_FILES and name in old_manifest['files'] for name in changes):
            raise InstallError('Для продолжения требуется исходная поставка незавершённого обновления.')
        if actual != journal.get('before'):
            raise InstallError('Манифест изменился после начала обновления. Сверьте его с журналом.')
        manifest = dict(old_manifest, files=old_manifest['files'] | changes)
        final = _json(manifest)
        if _digest(final) != journal.get('after') or any(_digest(contents[k]) != v for k, v in changes.items()):
            raise InstallError('Нужна та же поставка, с которой начато обновление. Исходный план сохранён.')
        for name, desired in changes.items():
            target = managed_path(project, name)
            current = _file_hash(target)
            if current == desired:
                continue
            prior = old_manifest['files'].get(name)
            if current != prior:
                raise InstallError(f'Во время обновления изменён {name}. Сверьте локальную правку с журналом {JOURNAL}.')
            if prior is None:
                if not publish_new(target, contents[name]):
                    raise InstallError(f'Путь {name} был занят во время обновления.')
            else:
                _replace(target, contents[name], prior, work, identifier)
        for name, desired in changes.items():
            if _file_hash(managed_path(project, name)) != desired:
                raise InstallError(f'Файл {name} изменился до записи манифеста. Локальная версия сохранена.')
        _replace(path, final, actual, work, identifier)
    plain_path(work / (identifier + '.tmp')).unlink(missing_ok=True)
    plain_path(project / JOURNAL).unlink()


def check_pi_stopped(project):
    """Копия проекта может содержать квитанции работающего процесса из исходной папки."""
    from .runtimes.pi_rpc import process_identity
    service = plain_path(project / '.pi/kit-service/connection.json')
    registry = plain_path(project / '.pi/kit-service/registry.json')
    if service.is_file():
        owner = json.loads(service.read_bytes())
        if owner.get('project') == str(project) and process_identity(owner['pid']) == owner['identity']:
            raise InstallError('Перед обновлением остановите службу этого проекта через kit.run_pi --stop-service.')
    if registry.is_file():
        record = json.loads(registry.read_bytes())
        for session in record.get('sessions', {}).values():
            process = session.get('process') or {}
            if (session.get('interactive') and process.get('pid') and process.get('identity')
                    and process_identity(process['pid']) == process['identity']):
                raise InstallError('Перед обновлением закройте интерактивную сессию Pi командой /quit.')


def update_project(source: Path, destination: str, *, apply=False) -> dict:
    project = project_path(destination, source)
    def origin():
        return MANIFEST if plain_path(project / MANIFEST).is_file() else PI_MANIFEST

    if not plain_path(project / origin()).is_file():
        raise InstallError('Манифест установки отсутствует. Для нового проекта используйте установщик.')
    agent = project_agent(project)
    contents = project_payload(source, agent)
    journal = plain_path(project / JOURNAL)
    if not apply:
        plan = _plan(project, contents, origin())
        return dict(project=str(project), status='interrupted' if journal.exists() else 'plan',
                    migration=origin() == PI_MANIFEST, agent=agent,
                    pending=str(journal) if journal.exists() else None, **plan.report)
    with operation_lock(project, 'chief-mode-switch'), operation_lock(project, 'kit-distribution-update'):
        migrating = origin() == PI_MANIFEST
        if migrating:
            previous = _plan(project, contents, PI_MANIFEST)
            if previous.report['conflicts']:
                return dict(project=str(project), status='conflicts', migration=True, agent=agent, **previous.report)
            check_pi_stopped(project)
            # Исходный манифест остаётся квитанцией прежней установки. Общий обновитель
            # теперь может продолжить после сбоя, даже если успел заменить часть файлов.
            if not publish_new(project / MANIFEST, _json(dict(previous.manifest, agent='pi'))):
                raise InstallError('Манифест уже изменён. Повторите просмотр плана.')
        plan = _plan(project, contents)
        resumed = journal.exists()
        if resumed:
            _finish(project, json.loads(journal.read_text(encoding='utf-8')), plan.manifest, contents)
            plan = _plan(project, contents)
        changes = plan.changed
        if changes:
            transaction = _transaction(plan, uuid4().hex)
            if not publish_new(journal, _json(transaction)):
                raise InstallError('Уже начато другое обновление. Повторите команду для сверки.')
            _finish(project, transaction, plan.manifest, contents)
        return dict(project=str(project), status='conflicts' if plan.report['conflicts'] else 'applied',
                    resumed=resumed, migration=migrating, agent=agent, **plan.report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='Полный путь к ранее установленному проекту.')
    parser.add_argument('--apply', action='store_true', help='Применить план и сохранить локальные изменения.')
    args = parser.parse_args(argv)
    try:
        report = update_project(Path(__file__).resolve().parents[1], args.project, apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report['conflicts'] or report['status'] == 'interrupted' else 0
    except (OSError, ValueError) as error:
        print(json.dumps(dict(status='error', error=str(error),
                              recovery='Проверьте причину и повторите с той же поставкой. Запустите без --apply для просмотра состояния.'),
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
