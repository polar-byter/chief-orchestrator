"""Единая точка входа в мастер установки и обновления проекта."""
import argparse
import json
from pathlib import Path

from kit import installer, update_project
from kit.installer_ui import main

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', choices=installer.AGENTS)
    parser.add_argument('--update', metavar='PROJECT')
    parser.add_argument('--preview', action='store_true', help='Показать изменения без записи')
    options = parser.parse_args()
    if options.update:
        try:
            report = update_project.update_project(Path(__file__).resolve().parent, options.update, apply=not options.preview)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            raise SystemExit(2 if report['conflicts'] or report['status'] == 'interrupted' else 0)
        except (ValueError, OSError) as error:
            parser.exit(1, str(error) + '\n')
    if options.preview:
        parser.error('--preview требует --update PROJECT')
    raise SystemExit(main(agent=options.agent))
