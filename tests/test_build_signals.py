"""Exercise termination of a real CLI process while its child is running."""

import os
import json
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

CLEANUP_HARNESS = r"""
import json, os, signal, subprocess, sys, tempfile
from pathlib import Path
from unittest.mock import patch
from evalve import build, cli, EvalveError
from test_build import Programs, VERSION

root = Path(sys.argv[1])
mode = sys.argv[2]
tempfile.tempdir = str(root)
programs = Programs()
real_run = build.run_program
real_cleanup = build.Engine.cleanup
real_directory_cleanup = tempfile.TemporaryDirectory.cleanup
gate = "import sys,time; from pathlib import Path; root=Path(sys.argv[1]); (root/'ready').touch(); exec(\"while not (root/'release').exists(): time.sleep(0.01)\")"
def run(argv, **kwargs):
    if argv[1:2] == ['build']:
        (root / 'context').write_text(argv[-1])
        if mode == 'failure':
            raise EvalveError('original build failure')
    if argv[1:3] in (['container', 'rm'], ['image', 'rm']):
        if mode != 'directory-cleanup' and not (root / 'ready').exists():
            real_run([sys.executable, '-c', gate, str(root)])
        with (root / 'attempted').open('a') as stream:
            stream.write(argv[-1] + '\n')
        if mode == 'cleanup-failure' and argv[1] == 'image':
            return subprocess.CompletedProcess(argv, 1, '', 'engine unavailable')
    if mode == 'cleanup-failure' and argv[1:3] == ['image', 'ls'] and 'evalve-build-' in argv[-1]:
        return subprocess.CompletedProcess(argv, 1, '', 'engine unavailable')
    return programs(argv, **kwargs)
def cleanup(engine):
    (root / 'expected').write_text(json.dumps([*engine.containers, *engine.images]))
    return real_cleanup(engine)
def directory_cleanup(directory):
    if mode == 'directory-cleanup' and not (root / 'ready').exists():
        real_run([sys.executable, '-c', gate, str(root)])
    return real_directory_cleanup(directory)
with patch.dict(os.environ, {'KITARU_API_URL': 'http://localhost'}), patch.object(build.shutil, 'which', side_effect=lambda name: name if name in ('git', 'docker') else None), patch.object(build, 'run_program', side_effect=run), patch.object(build, 'server_version', return_value=VERSION), patch.object(build.Engine, 'cleanup', cleanup), patch.object(tempfile.TemporaryDirectory, 'cleanup', directory_cleanup):
    raise SystemExit(cli.main(['build', '--agent', 'signal-fixture', '--command', 'python', '--no-push']))
"""


@unittest.skipUnless(os.name == "posix", "POSIX termination signals")
class BuildSignalsTest(unittest.TestCase):
    def test_signals_during_cleanup_finish_all_removals_and_report_the_outcome(self):
        signals = [signal.SIGTERM, signal.SIGINT]
        if hasattr(signal, "SIGHUP"):
            signals.append(signal.SIGHUP)
        for mode in ("success", "failure", "cleanup-failure", "directory-cleanup"):
            for signum in signals:
                with (
                    self.subTest(mode=mode, signal=signum),
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
                        [sys.executable, "-c", CLEANUP_HARNESS, str(root), mode],
                        env=environment,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                    )
                    try:
                        deadline = time.monotonic() + 10
                        while (
                            not (root / "ready").exists()
                            and process.poll() is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.02)
                        self.assertTrue(
                            (root / "ready").exists(), "Cleanup did not start"
                        )
                        process.send_signal(signum)
                        time.sleep(0.05)
                        if process.poll() is None:
                            process.send_signal(signal.SIGINT)
                        (root / "release").touch()
                        stdout, stderr = process.communicate(timeout=10)
                        expected = set(json.loads((root / "expected").read_text()))
                        attempted = (
                            set((root / "attempted").read_text().splitlines())
                            if (root / "attempted").exists()
                            else set()
                        )
                        self.assertEqual(attempted, expected, stderr)
                        self.assertFalse(Path((root / "context").read_text()).exists())
                        self.assertEqual(process.returncode, 128 + signum, stderr)
                        if mode == "failure":
                            self.assertIn("original build failure", stderr)
                        else:
                            self.assertRegex(
                                stdout + stderr,
                                "Validated local image retained: evalve-agent-image:",
                            )
                        if mode == "cleanup-failure":
                            self.assertIn("Could not clean up", stderr)
                    finally:
                        (root / "release").touch()
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.communicate(timeout=10)

    def test_termination_signals_stop_children_and_clean_resources(self):
        signals = [signal.SIGTERM, signal.SIGINT]
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
