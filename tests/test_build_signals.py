"""Exercise termination of a real CLI process while its child is running."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HARNESS = r"""
import os, signal, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from evalve import build, cli
from test_build import Programs, VERSION

root = Path(sys.argv[1])
tempfile.tempdir = str(root)
for name in ('SIGTERM', 'SIGHUP'):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
programs = Programs()
real_run = build.run_program
child = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
def run(argv, **kwargs):
    if argv[0] == 'docker' and argv[1] == 'build':
        (root / 'context').write_text(argv[-1])
        return real_run([sys.executable, '-c', child, str(root / 'child.pid')])
    if argv[1:3] == ['image', 'rm']:
        with (root / 'cleaned').open('a') as stream:
            stream.write(argv[-1] + '\n')
    return programs(argv, **kwargs)
with patch.dict(os.environ, {'KITARU_API_URL': 'http://localhost'}), patch.object(build.shutil, 'which', side_effect=lambda name: name if name in ('git', 'docker') else None), patch.object(build, 'run_program', side_effect=run), patch.object(build, 'server_version', return_value=VERSION):
    raise SystemExit(cli.main(['build', '--agent', 'signal-fixture', '--command', 'python', '--no-push']))
"""


@unittest.skipUnless(os.name == "posix", "POSIX termination signals")
class BuildSignalsTest(unittest.TestCase):
    def test_sigterm_and_sighup_stop_children_and_clean_resources(self):
        signals = [signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for signum in signals:
            with (
                self.subTest(signal=signum),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                environment = dict(
                    os.environ,
                    PYTHONPATH=os.pathsep.join(
                        [str(REPO / "src"), str(REPO / "tests")]
                    ),
                )
                process = subprocess.Popen(
                    [sys.executable, "-c", HARNESS, str(root)],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                child_pid = None
                try:
                    deadline = time.monotonic() + 10
                    while (
                        not (root / "child.pid").exists()
                        and process.poll() is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.02)
                    self.assertTrue(
                        (root / "child.pid").exists(), "CLI child did not start"
                    )
                    child_pid = int((root / "child.pid").read_text())
                    process.send_signal(signum)
                    # A pipe-owning orphan would keep communicate blocked.
                    process.wait(timeout=10)
                    self.assertTrue(
                        (root / "cleaned").exists(),
                        "Temporary image cleanup did not run",
                    )
                    self.assertFalse(
                        Path((root / "context").read_text()).exists(),
                        "Extracted source survived termination",
                    )
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                    self.assertEqual(process.returncode, 128 + signum)
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # The fixed runner gives the child its own process group.
                    if child_pid is not None:
                        try:
                            os.killpg(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            try:
                                os.kill(child_pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
