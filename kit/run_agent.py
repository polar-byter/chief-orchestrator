"""Запускает выбранный CLI с обычными настройками проекта или с включённым Чифом."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .chief_mode import mode_state
from .opencode_cli import is_wrapper, locate_opencode
from .pi_configuration import PI_PACKAGE, locate_pi_command
from .runtimes import claude_chief, codex_chief


def ordinary_command(root, agent):
    if agent == 'pi' and (os.environ.get('PI_KIT_COMMAND') or (root / '.pi/installation.json').is_file()):
        return locate_pi_command(root)
    installed = shutil.which(agent)
    if agent == 'pi':
        if installed and os.name == 'nt' and Path(installed).suffix.lower() == '.cmd':
            # У npm-поставки можно передать аргументы прямо Node, сохранив их границы.
            cli = Path(installed).parent / 'node_modules' / PI_PACKAGE / 'dist/bundle/cli.js'
            node = shutil.which('node')
            if node and cli.is_file():
                return [node, str(cli)]
        return [installed] if installed else locate_pi_command(root)
    if agent == 'opencode' and installed and is_wrapper(installed):
        # Старый ярлык Desktop может ссылаться на удалённую версию. По возможности
        # запускаем текущий исполняемый файл напрямую с отдельными аргументами.
        return [locate_opencode() or installed]
    if installed:
        return [installed]
    if agent == 'opencode':
        executable = locate_opencode()
    elif agent == 'codex':
        executable = codex_chief._executable()
    else:
        executable = claude_chief._executable()
    if not executable:
        raise ValueError('Установленный CLI не найден: ' + agent)
    return [str(executable)]


def launch(root, agent, arguments):
    root = Path(root).resolve()
    status = mode_state(root)['status']
    if status not in ('on', 'off'):
        raise ValueError('Сначала завершите переключение режима Чифа.')
    if status == 'on' and agent in ('opencode', 'pi'):
        name = 'run_opencode' if agent == 'opencode' else 'run_pi'
        command = [sys.executable, '-m', 'kit.' + name]
        if arguments and agent == 'opencode':
            command.append('--')
    else:
        command = ordinary_command(root, agent)
    return subprocess.call([*command, *arguments], cwd=root)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from .installer import project_agent
    if argv[:1] == ['--project-default']:
        try:
            return launch(Path.cwd(), project_agent(Path.cwd()), argv[1:])
        except (ValueError, OSError) as error:
            print(error, file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('agent', nargs='?', choices=('opencode','pi','codex','claude'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
        return launch(args.root, args.agent or project_agent(args.root), arguments)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
