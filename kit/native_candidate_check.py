"""Проверка одного завершённого кандидата неизменяемыми тестами его поручения."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
import time
from uuid import UUID

from .runtimes import native_process
from .runtimes.native_worker import MAX_FILE
from .suite_runner import CATEGORY_FAILED, CATEGORY_PASSED, final_status, run_category
from .task_control_state import plain_path


CHECK_TIMEOUT = 120
TEST_ID = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z', re.ASCII)
RUNTIME_NAMES = {'python', 'python3', 'python.exe', 'python3.exe'}


def test_selection(command, contract_names):
    """Выбираем тест из замороженных файлов. Команда анкеты не исполняется оболочкой."""
    if (not isinstance(command, (tuple, list)) or len(command) < 4
            or not all(isinstance(value, str) for value in command)):
        raise ValueError('Для проверки укажите python -m unittest и тестовый модуль.')
    python = Path(command[0]).name.lower()
    if python not in RUNTIME_NAMES and command[0] != sys.executable:
        raise ValueError('В этом адаптере доступен тест Python unittest.')
    if tuple(command[1:3]) != ('-m', 'unittest'):
        raise ValueError('Команда проверки должна вызывать модуль unittest.')
    names = [value for value in command[3:] if value != '-v']
    if names == ['discover']:
        if not any(Path(name).name.startswith('test') and name.endswith('.py') for name in contract_names):
            raise ValueError('Для поиска unittest передайте тестовый файл в контрактах.')
        return names
    modules = {name[:-3].replace('/', '.') for name in contract_names if name.endswith('.py')}
    if not names or len(names) > 64:
        raise ValueError('Укажите от одного до 64 тестов unittest.')
    selected = []
    for name in names:
        if name.endswith('.py'):
            name = name[:-3].replace('\\','/').replace('/','.')
        if not TEST_ID.fullmatch(name) or not any(name == module or name.startswith(module + '.') for module in modules):
            raise ValueError('Проверка должна ссылаться на замороженный тест из --contract.')
        selected.append(name)
    return selected


def validate_command(command, contracts):
    return ['python', '-m', 'unittest', *test_selection(command, contracts)]


def _copy_input(workspace, destination, name, expected_hash):
    if (not isinstance(name, str) or any(part in ('', '.', '..') for part in name.split('/'))
            or ':' in name or '\\' in name):
        raise ValueError('В снимке обнаружен недопустимый путь.')
    source = plain_path(workspace / name)
    with source.open('rb') as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE or sha256(data).hexdigest() != expected_hash:
        raise ValueError('Содержимое рабочей копии разошлось с журналом поручения.')
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _run_check(folder, selected, assigned, *, admission, should_stop, boundary):
    runner = folder / 'run_tests.py'
    shutil.copyfile(Path(__file__).parent / 'runtimes/candidate_unittest.py', runner)
    sandbox = folder / 'scratch'
    sandbox.mkdir()
    # CreateProcessW с новым AppContainer требует LOCALAPPDATA для инициализации профиля.
    system_keys = ('SystemRoot', 'WINDIR', 'SystemDrive', 'LOCALAPPDATA', 'LANG', 'LC_ALL')
    env = {key: os.environ[key] for key in system_keys if key in os.environ}
    deadline = time.monotonic() + CHECK_TIMEOUT

    def stopped():
        return should_stop() or time.monotonic() >= deadline
    with TemporaryFile() as output, TemporaryFile() as errors:
        def arguments(python):
            return [str(python), '-I', '-S', '-B', str(runner), str(folder / 'source'), assigned, *selected]
        if os.name == 'nt':
            from .runtimes import windows_job
            from .runtimes.windows_check_container import copy_python, read_only_container
            python = copy_python(folder / 'runtime')
            with read_only_container(folder, UUID(boundary).hex) as capabilities:
                code = windows_job.run(arguments(python), name=UUID(boundary).hex, cwd=sandbox, env=env,
                                       streams=(None, output, errors), should_stop=stopped, admission=admission,
                                       app_container=capabilities)
        else:
            code = native_process.run_owned(arguments(sys.executable), cwd=sandbox, env=env,
                                            stdin=None, stdout=output, stderr=errors, boundary=boundary,
                                            admission=admission, should_stop=stopped)
        if code or stopped():
            raise ValueError('Изолированный запуск не вернул результат теста.')
        output.seek(0)
        raw = output.read(8193)
        if len(raw) > 8192:
            raise ValueError('Некорректный размер ответа проверки.')
        return json.loads(raw)


def verify(state, job, plan, *, admission, should_stop, boundary):
    """Результат относится к конкретному файлу, проверенным контрактам и запуску группы ОС."""
    begun = time.monotonic()
    names = {item['path']: item['sha256'] for item in plan['contracts']}
    result = dict(verdict='blocked', tests=0, failures=0, errors=0, skipped=0, test_name='',
                  candidate_hash=job['candidate_hash'], test_hashes=names, diagnostic='')
    try:
        workspace = plain_path(Path(job['workspace']))
        if workspace != plain_path(state.directory / 'workspaces' / str(UUID(job['id']))):
            raise ValueError('Рабочая копия не принадлежит этому поручению.')
        assigned = job['assigned_file']
        if assigned != plan['assigned']['path']:
            raise ValueError('Поручение и план указывают на разные файлы.')
        selected = test_selection(plan['verification']['command'], names)
        with TemporaryDirectory(prefix='chief-check-') as temp:
            folder = Path(temp)
            for name, digest in names.items():
                _copy_input(workspace, folder / 'source', name, digest)
            _copy_input(workspace, folder / 'source', assigned, job['candidate_hash'])
            outcome = _run_check(folder, selected, assigned, admission=admission,
                                 should_stop=should_stop, boundary=boundary)
        fields = ('tests', 'failures', 'errors', 'skipped', 'unexpected_successes', 'expected_failures')
        if (not isinstance(outcome, dict)
                or any(type(outcome.get(key)) is not int or outcome[key] < 0 for key in fields)):
            raise ValueError('Неполный результат изолированного теста.')
        name = outcome.get('test_name')
        if isinstance(name, str) and len(name) <= 220 and TEST_ID.fullmatch(name):
            result['test_name'] = name
        result.update({key: outcome[key] for key in fields[:4]})
        code = int(bool(outcome['failures'] or outcome['errors'] or outcome['unexpected_successes']))
        counts = dict(result, status=final_status(result, code))
        category = run_category(counts, code)
        incomplete = outcome['expected_failures'] or outcome['unexpected_successes'] or outcome['skipped']
        if outcome['errors'] or outcome.get('load_errors') is not False:
            result['diagnostic'] = 'Ошибка загрузки или исполнения теста. Требуется проверка окружения.'
        elif incomplete or not outcome['tests'] or outcome.get('observed') is not True:
            result['diagnostic'] = 'Нужен полный запуск теста, обращающегося к назначенному файлу.'
        elif category == CATEGORY_FAILED and outcome['failures']:
            result.update(verdict='failed', diagnostic='Не выполнено утверждение unittest' +
                          (' · ' + result['test_name'] if result['test_name'] else '') + '.')
        elif category == CATEGORY_PASSED:
            result.update(verdict='passed', diagnostic='Все проверки назначенного файла прошли.')
        else:
            result['diagnostic'] = 'Отсутствует подтверждённый результат теста.'
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        result['diagnostic'] = str(error)[:250] if type(error) is ValueError else 'Не удалось выполнить изолированную проверку.'
    result['duration_seconds'] = round(time.monotonic() - begun, 3)
    return result
