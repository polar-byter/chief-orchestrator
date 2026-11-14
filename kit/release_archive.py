"""Сборка локального ZIP Чифа из того же состава, который получает установщик."""
import argparse
from hashlib import sha256
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

from .installer import payload
from .task_control_state import plain_path


def build(source, destination, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?', version):
        raise ValueError('Укажите номер версии, например 1.2.0 или 1.2.0-rc.1.')
    source, destination = Path(source).resolve(), Path(destination)
    contents = payload(source)
    for name in ('LICENSE', 'README.md', 'README.en.md', 'AGENTS.md'):
        contents[name] = plain_path(source / name).read_bytes()
    # Иллюстрации остаются рядом с README в ZIP. В рабочий проект мастер копирует только комплект.
    illustrations = {name for guide in ('README.md', 'README.en.md')
                     for name in re.findall(r'\((readme_gif/[A-Za-z0-9_./-]+)\)', contents[guide].decode())}
    for name in illustrations:
        path = plain_path(source / name)
        if '..' in Path(name).parts:
            raise ValueError('Некорректная ссылка на иллюстрацию в README.')
        contents[name] = path.read_bytes()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / ('chief-' + version + '.zip')
    with NamedTemporaryFile(dir=destination, suffix='.zip', delete=False) as temp:
        work = Path(temp.name)
    try:
        with ZipFile(work, 'w', compression=ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in sorted(contents.items()):
                archive.writestr(name, data)
        with ZipFile(work) as archive:
            if set(archive.namelist()) != contents.keys() or any(archive.read(k) != v for k, v in contents.items()):
                raise ValueError('Состав или содержимое ZIP расходится с поставкой.')
        checksum = sha256(work.read_bytes()).hexdigest()
        # В Windows rename откажет при занятом имени, в том числе на диске без жёстких ссылок.
        if os.name == 'nt':
            work.rename(target)
        else:
            target.hardlink_to(work)
        return target, checksum
    finally:
        work.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        path, checksum = build(Path(__file__).resolve().parents[1], args.output, args.version)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'{checksum}  {path}')


if __name__ == '__main__':
    main()
