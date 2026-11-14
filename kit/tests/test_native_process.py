"""Подтверждение остановки по объекту ОС, включая потомков завершившегося CLI."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from uuid import uuid4

from kit.runtimes import native_process
from kit.runtimes.pi_rpc import process_identity


# Три поколения, у младшего свой сеанс на Linux. Обычный обход PPID потеряет
# потомка после завершения промежуточного процесса.
TREE = """\
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path.cwd()
role = sys.argv[1]
if role == 'leaf':
    (root/'leaf').write_text(str(os.getpid()))
    while True:
        with (root/'heartbeat').open('ab') as file:
            file.write(b'.')
        time.sleep(.04)
elif role == 'child':
    subprocess.Popen([sys.executable, __file__, 'leaf'], start_new_session=os.name != 'nt')
else:
    subprocess.Popen([sys.executable, __file__, 'child']).wait()
    while not (root/'heartbeat').is_file():
        time.sleep(.02)
    if role == 'linger':
        time.sleep(60)
"""
HOST = """\
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from kit.runtimes.native_process import run_owned
sys.exit(run_owned([sys.executable, 'tree.py', 'linger'], cwd=Path.cwd(), env=dict(os.environ),
                   stdin=None, stdout=None, stderr=None, boundary=sys.argv[2], should_stop=lambda:False))
"""


def wait_until(check):
    deadline = time.monotonic() + 10
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError('Не дождались перехода состояния группы процессов.')
        time.sleep(.03)


class ProcessGroupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not native_process.supported():
            raise unittest.SkipTest('В этой среде нет подтверждённой группы Windows Job Object или systemd/cgroup v2.')

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        (self.root / 'tree.py').write_text(TREE, encoding='utf-8')
        self.boundary = uuid4().hex
        self.addCleanup(native_process.stop_and_confirm, self.boundary)

    def run_role(self, role, stopping=lambda:False, admission=nullcontext):
        return native_process.run_owned([sys.executable, 'tree.py', role], cwd=self.root, env=os.environ.copy(),
                                        stdin=None, stdout=None, stderr=None, boundary=self.boundary,
                                        should_stop=stopping, admission=admission)

    def test_grandchild_stops_and_an_unrelated_process_keeps_running(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'],
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(self.run_role, 'linger')
                wait_until(lambda: (self.root / 'heartbeat').is_file())
                leaf = int((self.root / 'leaf').read_text())
                self.assertIsNotNone(process_identity(leaf))
                self.assertTrue(native_process.stop_and_confirm(self.boundary))
                self.assertNotEqual(running.result(timeout=12), 0)
                mark = (self.root / 'heartbeat').stat().st_size
                time.sleep(.18)
                self.assertEqual(mark, (self.root / 'heartbeat').stat().st_size)
                wait_until(lambda: process_identity(leaf) is None)
            self.assertIsNone(other.poll())
        finally:
            other.terminate()
            other.wait(timeout=5)

    def test_completion_of_parent_reaps_long_lived_grandchild(self):
        self.assertEqual(self.run_role('return'), 0)
        leaf = int((self.root / 'leaf').read_text())
        # cgroup.events может перейти в populated=0 чуть раньше, чем исчезнет /proc/PID.
        wait_until(lambda: process_identity(leaf) is None)

    def test_controller_crash_has_a_confirmable_group(self):
        (self.root / 'host.py').write_text(HOST, encoding='utf-8')
        host = subprocess.Popen([sys.executable, 'host.py', str(Path(__file__).resolve().parents[2]), self.boundary],
                                cwd=self.root, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            wait_until(lambda: (self.root / 'heartbeat').is_file() or host.poll() is not None)
            self.assertIsNone(host.poll())
            leaf = int((self.root / 'leaf').read_text())
            host.kill()
            host.wait(timeout=5)
            if os.name == 'nt':
                wait_until(lambda: process_identity(leaf) is None)
            self.assertTrue(native_process.stop_and_confirm(self.boundary))
            wait_until(lambda: process_identity(leaf) is None)
        finally:
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)

    def test_queued_runner_checks_gate_after_controller_closed_it(self):
        lock = threading.Lock()
        gate_closed = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with lock:
                queued = pool.submit(self.run_role, 'linger', gate_closed.is_set, lambda:lock)
                gate_closed.set()
                self.assertTrue(native_process.stop_and_confirm(self.boundary))
            self.assertEqual(queued.result(timeout=12), 125)
        self.assertFalse((self.root / 'leaf').exists())

    def test_cli_receives_exact_arguments_environment_and_streams(self):
        env = dict(os.environ, CHIEF_OBSERVATION='ход')
        src, dst, err = (self.root / name for name in ('in.bin', 'out.bin', 'err.bin'))
        src.write_bytes(b'expected')
        script = ('import os,sys; assert sys.stdin.buffer.read()==b"expected"; '
                  'sys.stdout.buffer.write((os.environ["CHIEF_OBSERVATION"]+sys.argv[1]).encode()); '
                  'sys.stderr.buffer.write(b"done");sys.exit(7)')
        with src.open('rb') as s, dst.open('wb') as o, err.open('wb') as e:
            code = native_process.run_owned([sys.executable, '-c', script, ' + шаг'], env=env, cwd=self.root,
                                            stdin=s, stdout=o, stderr=e, boundary=self.boundary, should_stop=lambda:False)
        self.assertEqual((code, dst.read_text(encoding='utf-8'), err.read_bytes()), (7, 'ход + шаг', b'done'))


class GroupIdentityTests(unittest.TestCase):
    def test_only_generated_ids_can_name_an_os_container(self):
        for name in ('', 'system.slice', '../other', None):
            with self.assertRaises(ValueError):
                native_process.stop_and_confirm(name)
