"""Tests of the Supervisor of a Loop task.

Each case has a fake backend (`http.server`), a fake `kitaru` program and a
clock that the test controls. The path of the worker command, the directory
`/tmp/evalve`, the two directories that the sweep empties and `/proc` are
parameters, so the tests use temporary ones. No test reads the real `/proc`,
and no test sends a signal to a process that the test did not start.
"""

import ast
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from evalve.supervisor import supervisor

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "evalve" / "supervisor"
SECRET = "channel-secret-0123456789"
START = 1_800_000_000.0

# The fake `kitaru` program. A file `<worker ID>.plan` says what it does:
#   wait     (default) exit 0 when `<worker ID>.release` exists, or 143 at SIGTERM
#   drain    after SIGTERM, continue until `<worker ID>.release` exists, then exit 143
#   ignore   never exit
#   exit:N   exit at once with code N
#   child    start a child process in the same session, then exit 0
FAKE_KITARU = r"""
import json, os, signal, subprocess, sys, time

home = os.environ["FAKE_KITARU"]
name = os.environ["KITARU_WORKER_ID"]


def path(suffix):
    return os.path.join(home, name + suffix)


try:
    plan = open(path(".plan")).read().strip()
except FileNotFoundError:
    plan = "wait"
terms = []


def on_term(signum, frame):
    terms.append(signum)
    with open(path(".signals"), "a") as handle:
        handle.write("SIGTERM\n")


signal.signal(signal.SIGTERM, on_term)
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
          "pid": os.getpid(), "sid": os.getsid(0)}
if plan == "child":
    record["child"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]).pid
with open(path(".starts"), "a") as handle:
    handle.write("start\n")
with open(path(".tmp"), "w") as handle:
    json.dump(record, handle)
os.replace(path(".tmp"), path(".json"))
if plan.startswith("exit:"):
    sys.exit(int(plan[5:]))
if plan == "child":
    sys.exit(0)
while True:
    released = os.path.exists(path(".release"))
    if plan == "wait" and terms:
        sys.exit(143)
    if plan == "wait" and released:
        sys.exit(0)
    if plan == "drain" and terms and released:
        sys.exit(143)
    time.sleep(0.02)
"""


class Backend:
    """The fake backend. It records each poll and gives the reply that the test set."""

    def __init__(self):
        self.requests = []  # (path, Authorization header, body)
        self.reply = (200, {"run_deadline": None, "assignments": []})
        self.delay = 0
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                backend.requests.append((self.path, self.headers.get("Authorization"), body))
                time.sleep(backend.delay)
                status, reply = backend.reply
                data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # The Supervisor closed the connection at its timeout.

        self.server = Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/evalve"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def bodies(self):
        return [body for _, _, body in self.requests]


class FakeClock:
    def __init__(self):
        self.time = START

    def now(self):
        return self.time

    def sleep(self, seconds):
        self.time += seconds


def until(condition, seconds=10):
    """Wait in real time for something that a real process does."""
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("The condition did not become true in time.")
        time.sleep(0.02)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class SupervisorCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "evalve")
        self.replays = os.path.join(self.root, "replays")
        self.shm = os.path.join(self.tmp, "shm")
        self.mqueue = os.path.join(self.tmp, "mqueue")
        self.proc = os.path.join(self.tmp, "proc")
        self.fake = os.path.join(self.tmp, "fake")
        for directory in (self.root, self.shm, self.mqueue, self.proc, self.fake):
            os.makedirs(directory)
        self.kitaru = os.path.join(self.tmp, "kitaru")
        with open(self.kitaru, "w") as handle:
            handle.write(f"#!{sys.executable}\n{FAKE_KITARU}")
        os.chmod(self.kitaru, 0o755)
        self.backend = Backend()
        self.addCleanup(self.backend.close)
        self.clock = FakeClock()
        self.out = io.StringIO()
        self.started = []
        self.addCleanup(self.kill_what_is_left)

    def kill_what_is_left(self):
        for item in self.started:
            for worker in item.workers.values():
                try:
                    os.killpg(worker.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                worker.process.wait(timeout=10)

    def environ(self, **changes):
        environ = {
            "PATH": os.environ.get("PATH", ""),
            "FAKE_KITARU": self.fake,
            "EVALVE_BACKEND_URL": self.backend.url,
            "EVALVE_LOOP_ID": "loop-1",
            "EVALVE_LOOP_TASK_SECRET": SECRET,
            "KITARU_API_URL": "https://kitaru.example.com",
            "EVALVE_MAX_REPLAYS": "10",
            "EVALVE_POLL_INTERVAL_SECONDS": "2",
            "EVALVE_IDLE_TIMEOUT_SECONDS": "900",
            "EVALVE_SOFT_LIFETIME_SECONDS": "86400",
        }
        environ.update(changes)
        return {name: value for name, value in environ.items() if value is not None}

    def options(self):
        return dict(
            worker_command=self.kitaru,
            root=self.root,
            runtime_directories=(self.shm, self.mqueue),
            proc_path=self.proc,
        )

    def supervisor(self, poll_timeout=5, **changes):
        environ = self.environ(**changes)
        item = supervisor.Supervisor(
            supervisor.Settings(environ), environ, clock=self.clock, out=self.out,
            poll_timeout=poll_timeout, **self.options(),
        )  # fmt: skip
        self.started.append(item)
        return item

    def assign(self, *jobs, run="run-1", deadline=3600):
        """Set the reply: one Assignment for each job, and the Run deadline in seconds from now."""
        moment = datetime.fromtimestamp(self.clock.time + deadline, timezone.utc)
        self.backend.reply = (200, {
            "run_deadline": None if deadline is None else moment.isoformat(),
            "assignments": [
                {"run_id": run, "job_id": job, "worker_id": f"worker-{job}",
                 "worker_token": f"token-of-{job}"}
                for job in jobs
            ],
        })  # fmt: skip

    def file(self, job, suffix):
        return os.path.join(self.fake, f"worker-{job}{suffix}")

    def plan(self, job, plan):
        with open(self.file(job, ".plan"), "w") as handle:
            handle.write(plan)

    def release(self, job):
        open(self.file(job, ".release"), "w").close()

    def record(self, job):
        """What the fake `kitaru` program of a job saw at its start."""
        until(lambda: os.path.exists(self.file(job, ".json")))
        with open(self.file(job, ".json")) as handle:
            return json.load(handle)

    def lines(self, job, suffix):
        try:
            with open(self.file(job, suffix)) as handle:
                return handle.read().split()
        except FileNotFoundError:
            return []

    def starts(self, job):
        return len(self.lines(job, ".starts"))

    def events(self, name=None):
        events = [json.loads(line) for line in self.out.getvalue().splitlines()]
        return [event for event in events if name in (None, event["event"])]

    def after(self, item, seconds):
        """Move the clock, then let the Supervisor do one step."""
        self.clock.time += seconds
        item.step()

    def step_until_exit(self, item, job):
        """Do steps with no change of the clock until the worker process of a job exited."""

        def exited():
            item.step()
            return any(event["job_id"] == job for event in self.events("worker_exit"))

        until(exited)


class PollTest(SupervisorCase):
    def test_a_poll_has_the_url_the_header_and_the_body(self):
        self.supervisor().step()
        self.assertEqual(
            self.backend.requests,
            [(
                "/evalve/v1/loops/loop-1/task/poll",
                f"EvalveLoopTask {SECRET}",
                {"contract_version": 1, "last": False},
            )],
        )  # fmt: skip

    def test_the_first_poll_is_immediate_and_the_next_follow_at_the_interval(self):
        item = self.supervisor()
        item.step()
        item.step()
        self.assertEqual(len(self.backend.requests), 1)
        self.after(item, 1.9)
        self.assertEqual(len(self.backend.requests), 1)
        self.after(item, 0.1)
        self.assertEqual(len(self.backend.requests), 2)

    def test_a_401_and_a_409_each_stop_the_workers_and_give_exit_code_3(self):
        for status in (401, 409):
            with self.subTest(status=status):
                self.backend.requests.clear()
                job = f"job-{status}"
                self.plan(job, "ignore")
                self.assign(job)
                item = self.supervisor()
                item.step()
                pid = self.record(job)["pid"]
                self.backend.reply = (status, {"detail": "refused"})
                self.after(item, 2)
                self.assertEqual(item.exit_code, 3)
                self.assertFalse(alive(pid))
                self.assertEqual(os.listdir(self.replays), [])
                self.assertEqual(self.events("poll_refused")[-1]["status"], status)
                # It sends no last poll.
                self.assertEqual([body["last"] for body in self.backend.bodies], [False, False])

    def test_a_500_a_timeout_and_a_reply_that_cannot_be_read_change_nothing(self):
        failures = {
            "a 500": dict(reply=(500, {"detail": "error"})),
            "a timeout": dict(delay=0.6),
            "a reply that is not JSON": dict(reply=(200, b"not json")),
            "a reply with no list": dict(reply=(200, {"run_deadline": None, "assignments": "x"})),
        }
        self.assign("a")
        good = self.backend.reply
        item = self.supervisor(poll_timeout=0.2)
        item.step()
        pid = self.record("a")["pid"]
        for name, failure in failures.items():
            with self.subTest(name):
                self.backend.reply = failure.get("reply", good)
                self.backend.delay = failure.get("delay", 0)
                polls, failed = len(self.backend.requests), len(self.events("poll_failed"))
                self.after(item, 2)
                self.assertIsNone(item.exit_code)
                self.assertTrue(alive(pid))
                self.assertEqual(len(self.events("poll_failed")), failed + 1)
                # The next poll is made.
                self.backend.reply, self.backend.delay = good, 0
                self.after(item, 2)
                self.assertEqual(len(self.backend.requests), polls + 2)
        self.assertEqual(self.starts("a"), 1)


class WorkerTest(SupervisorCase):
    def test_with_3_assignments_and_n_2_the_third_starts_after_one_exits(self):
        self.assign("a", "b", "c")
        item = self.supervisor(EVALVE_MAX_REPLAYS="2")
        item.step()
        self.record("a"), self.record("b")
        self.after(item, 2)
        self.assertEqual(self.starts("c"), 0)
        self.release("a")
        self.step_until_exit(item, "a")
        # The third waits for a later poll.
        self.assertEqual(self.starts("c"), 0)
        self.after(item, 2)
        self.record("c")
        self.assertEqual([self.starts(job) for job in "abc"], [1, 1, 1])

    def test_a_worker_process_gets_its_variables_its_directory_and_its_own_session(self):
        self.assign("a")
        self.supervisor(KITARU_API_KEY="an-account-key", OTHER="stays").step()
        record = self.record("a")
        directory = os.path.join(self.replays, "a")
        env = record["env"]
        expected = {
            "KITARU_API_URL": "https://kitaru.example.com",
            "KITARU_WORKER_ID": "worker-a",
            "KITARU_API_TOKEN": "token-of-a",
            "HOME": directory,
            "TMPDIR": directory,
            "UV_CACHE_DIR": os.path.join(self.root, "cache", "uv"),
            "KITARU_WORKER_BLOB_CACHE_ROOT": os.path.join(self.root, "cache", "kitaru-blobs"),
            "KITARU_WORKER_PAYLOAD_CACHE_ROOT": os.path.join(self.root, "cache", "kitaru-payloads"),
            "KITARU_DISABLE_CLIENT_ANALYTICS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OTHER": "stays",
        }
        self.assertEqual({name: env.get(name) for name in expected}, expected)
        self.assertNotIn("EVALVE_LOOP_TASK_SECRET", env)
        self.assertNotIn("KITARU_API_KEY", env)
        self.assertEqual(record["cwd"], directory)
        self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), 0o700)
        # The worker process is the leader of its own session.
        self.assertEqual(record["sid"], record["pid"])
        self.assertNotEqual(record["sid"], os.getsid(0))

    def test_the_timeout_is_the_number_of_seconds_to_the_run_deadline(self):
        self.assign("a", deadline=1234)
        self.supervisor().step()
        self.assertEqual(self.record("a")["argv"], ["worker", "start", "--timeout", "1234"])

    def test_the_run_deadline_can_end_with_z(self):
        self.assign("a")
        moment = datetime.fromtimestamp(START + 90, timezone.utc)
        self.backend.reply[1]["run_deadline"] = moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.supervisor().step()
        self.assertEqual(self.record("a")["argv"][-1], "90")

    def test_no_worker_starts_with_no_run_deadline_or_one_in_the_past(self):
        for deadline in (None, -1):
            with self.subTest(deadline=deadline):
                self.assign("a", deadline=deadline or 0)
                if deadline is None:
                    self.backend.reply[1]["run_deadline"] = None
                else:
                    self.clock.time += 1
                item = self.supervisor()
                item.step()
                self.assertEqual(item.workers, {})
                self.assertEqual(self.events("worker_start"), [])

    def test_after_an_exit_the_directory_is_gone_and_the_job_gets_no_second_start(self):
        self.plan("ok", "exit:0")
        self.plan("bad", "exit:3")
        self.assign("ok", "bad")
        item = self.supervisor()
        item.step()
        self.step_until_exit(item, "ok")
        self.step_until_exit(item, "bad")
        self.assertEqual(os.listdir(self.replays), [])
        # The poll gives the same Assignments until the Run is collected.
        self.after(item, 2)
        self.after(item, 2)
        self.assertEqual([self.starts("ok"), self.starts("bad")], [1, 1])
        exits = {event["job_id"]: event for event in self.events("worker_exit")}
        self.assertEqual([exits["ok"]["exit_code"], exits["bad"]["exit_code"]], [0, 3])
        self.assertIn("seconds", exits["ok"])

    def test_an_exit_kills_what_is_left_of_the_session(self):
        self.plan("a", "child")
        self.assign("a")
        item = self.supervisor()
        item.step()
        child = self.record("a")["child"]
        self.assertTrue(alive(child))
        self.step_until_exit(item, "a")
        until(lambda: not alive(child))

    def test_a_worker_that_runs_30_s_after_the_run_deadline_is_killed(self):
        self.plan("a", "ignore")
        self.assign("a", deadline=100)
        item = self.supervisor()
        item.step()
        pid = self.record("a")["pid"]
        self.after(item, 129)
        self.assertTrue(alive(pid))
        self.assertEqual(self.events("worker_deadline"), [])
        self.after(item, 1)
        self.step_until_exit(item, "a")
        self.assertEqual(self.events("worker_exit")[0]["exit_code"], -signal.SIGKILL)
        self.assertEqual(os.listdir(self.replays), [])

    def test_a_full_disk_gets_a_log_entry_and_the_start_is_tried_again(self):
        self.assign("a")
        item = self.supervisor()
        full = OSError(28, "No space left on device")
        with patch.object(supervisor.os, "makedirs", side_effect=full):
            item.step()
        self.assertEqual(self.events("disk_full")[0]["job_id"], "a")
        self.assertEqual(self.starts("a"), 0)
        # This did not count as a start.
        self.after(item, 2)
        self.record("a")
        self.assertEqual(self.starts("a"), 1)

    def test_a_directory_that_cannot_be_deleted_gets_a_log_entry(self):
        self.plan("a", "exit:0")
        self.assign("a", "b")
        item = self.supervisor()
        with patch.object(supervisor.shutil, "rmtree", side_effect=PermissionError(13, "denied")):
            item.step()
            self.step_until_exit(item, "a")
        failure = self.events("delete_failed")[0]
        self.assertEqual(failure["path"], os.path.join(self.replays, "a"))
        # The Supervisor continues.
        self.assertIsNone(item.exit_code)
        self.after(item, 2)
        self.assertEqual(len(self.backend.requests), 2)


class SweepTest(SupervisorCase):
    def fill(self, directory):
        """Put a file, a directory with a file and a link in a directory."""
        os.makedirs(os.path.join(directory, "sub"))
        for name in ("file", os.path.join("sub", "file")):
            open(os.path.join(directory, name), "w").close()
        os.symlink(self.tmp, os.path.join(directory, "link"))

    def test_a_new_run_id_starts_the_sweep_before_the_first_worker_of_the_new_run(self):
        self.plan("a", "ignore")
        self.plan("b", "ignore")
        self.fill(self.shm)
        self.fill(self.mqueue)
        cache = os.path.join(self.root, "cache", "uv")
        os.makedirs(cache)
        open(os.path.join(cache, "wheel"), "w").close()
        self.assign("a", "b", run="run-1")
        item = self.supervisor()
        item.step()
        pids = [self.record(job)["pid"] for job in "ab"]
        # The sweep does not run for the first Run.
        self.assertEqual(self.events("sweep"), [])
        self.assertEqual(sorted(os.listdir(self.shm)), ["file", "link", "sub"])

        self.assign("c", run="run-2")
        self.after(item, 2)
        self.record("c")
        self.assertEqual([alive(pid) for pid in pids], [False, False])
        self.assertEqual(os.listdir(self.replays), ["c"])
        self.assertEqual([os.listdir(self.shm), os.listdir(self.mqueue)], [[], []])
        # The link is deleted, and the directory that it names is not.
        self.assertTrue(os.path.isdir(self.fake))
        # The sweep does not delete the shared caches.
        self.assertEqual(os.listdir(cache), ["wheel"])
        names = [event["event"] for event in self.events()]
        self.assertLess(names.index("sweep"), names.index("worker_start", names.index("sweep")))
        self.assertEqual(self.events("sweep")[0]["entries"], 6)

    def test_the_sweep_does_not_run_when_the_list_of_assignments_is_empty(self):
        self.plan("a", "exit:0")
        self.assign("a", run="run-1")
        item = self.supervisor()
        item.step()
        self.step_until_exit(item, "a")
        self.fill(self.shm)
        self.assign()
        self.after(item, 2)
        self.assertEqual(self.events("sweep"), [])
        self.assertEqual(len(os.listdir(self.shm)), 3)
        # The Supervisor kept the Run ID: the next Run gets the sweep.
        self.assign("b", run="run-2")
        self.after(item, 2)
        self.assertEqual(len(self.events("sweep")), 1)
        self.assertEqual(os.listdir(self.shm), [])

    def test_the_sweep_kills_a_process_that_a_worker_left(self):
        left = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(left.kill)
        self.write_stat(os.getpid(), parent=1, session=os.getsid(0))
        self.write_stat(left.pid, parent=1, session=left.pid)
        self.plan("a", "exit:0")
        self.assign("a", run="run-1")
        item = self.supervisor()
        item.step()
        self.step_until_exit(item, "a")
        self.assertIsNone(left.poll())
        self.assign("b", run="run-2")
        self.after(item, 2)
        self.assertEqual(left.wait(timeout=10), -signal.SIGKILL)
        self.assertEqual(self.events("sweep")[0]["processes"], 1)

    def test_an_entry_that_the_sweep_cannot_delete_gets_a_log_entry(self):
        self.plan("a", "exit:0")
        self.assign("a", run="run-1")
        item = self.supervisor()
        item.step()
        self.step_until_exit(item, "a")
        open(os.path.join(self.shm, "locked"), "w").close()
        open(os.path.join(self.mqueue, "queue"), "w").close()
        unlink = os.unlink

        def refuse_one(path):
            if os.path.basename(path) == "locked":
                raise PermissionError(1, "Operation not permitted", path)
            unlink(path)

        self.assign("b", run="run-2")
        with patch.object(supervisor.os, "unlink", side_effect=refuse_one):
            self.after(item, 2)
        self.assertEqual(self.events("delete_failed")[0]["path"], os.path.join(self.shm, "locked"))
        # The Supervisor continues: it empties the other directory and starts the worker.
        self.assertEqual(os.listdir(self.mqueue), [])
        self.record("b")

    def write_stat(self, pid, parent, session, name="python3"):
        os.makedirs(os.path.join(self.proc, str(pid)))
        with open(os.path.join(self.proc, str(pid), "stat"), "w") as handle:
            handle.write(f"{pid} ({name}) S {parent} {pid} {session} 0 -1 4194560 0 0\n")

    def test_the_selection_leaves_out_the_supervisor_and_each_process_above_it(self):
        self.write_stat(1, parent=0, session=1, name="docker-init")
        self.write_stat(7, parent=1, session=1, name="timeout")
        self.write_stat(12, parent=7, session=1)
        self.write_stat(30, parent=12, session=30, name="kitaru")
        self.write_stat(31, parent=30, session=31, name="agent) (x y")
        self.write_stat(40, parent=1, session=31, name="left by a killed worker")
        os.makedirs(os.path.join(self.proc, "self"))
        open(os.path.join(self.proc, "uptime"), "w").close()
        self.assertEqual(supervisor.processes_to_sweep(self.proc, 12), [30, 31, 40])
        self.assertEqual(supervisor.processes_of_session(self.proc, 31), [31, 40])
        # The fake `/proc` has no `cmdline` files: the selection reads only `stat`.
        self.assertEqual(
            {name for _, _, names in os.walk(self.proc) for name in names}, {"stat", "uptime"}
        )


class IdleTest(SupervisorCase):
    def test_with_no_assignment_for_the_idle_timeout_the_supervisor_sends_the_last_poll(self):
        item = self.supervisor()
        item.step()
        self.after(item, 899)
        self.assertIsNone(item.exit_code)
        self.after(item, 1)
        self.assertEqual(item.exit_code, 0)
        self.assertEqual(self.backend.bodies[-1], {"contract_version": 1, "last": True})
        self.assertEqual([body["last"] for body in self.backend.bodies].count(True), 1)
        self.assertEqual(self.events("exit")[0]["reason"], "idle")

    def test_a_worker_process_that_runs_keeps_the_loop_task_not_idle(self):
        self.assign("a")
        item = self.supervisor(EVALVE_IDLE_TIMEOUT_SECONDS="10")
        item.step()
        self.record("a")
        self.after(item, 60)
        self.assertIsNone(item.exit_code)
        self.release("a")
        self.step_until_exit(item, "a")
        # The idle clock starts again when the worker process exited.
        self.after(item, 9)
        self.assertIsNone(item.exit_code)
        self.after(item, 1)
        self.assertEqual(item.exit_code, 0)

    def test_an_assignment_that_waits_keeps_the_loop_task_not_idle(self):
        self.assign("a", deadline=0)
        self.backend.reply[1]["run_deadline"] = None
        item = self.supervisor(EVALVE_IDLE_TIMEOUT_SECONDS="10")
        item.step()
        self.after(item, 60)
        self.assertIsNone(item.exit_code)

    def test_after_the_soft_limit_an_assignment_that_waits_still_gets_its_worker(self):
        self.assign("a", "b", deadline=7200)
        item = self.supervisor(EVALVE_MAX_REPLAYS="1", EVALVE_SOFT_LIFETIME_SECONDS="100")
        item.step()
        self.record("a")
        self.after(item, 200)
        self.assertIsNone(item.exit_code)
        self.release("a")
        self.step_until_exit(item, "a")
        self.assertIsNone(item.exit_code)
        self.after(item, 2)
        self.record("b")
        self.release("b")
        # The exit is at the first idle moment, with no wait for the idle timeout of 900 s.
        self.step_until_exit(item, "b")
        self.assertEqual(item.exit_code, 0)
        self.assertEqual(self.events("exit")[0]["reason"], "soft lifetime limit")
        self.assertTrue(self.backend.bodies[-1]["last"])


class SigtermTest(SupervisorCase):
    def test_on_sigterm_each_worker_drains_and_the_supervisor_polls_during_the_drain(self):
        self.plan("a", "drain")
        self.plan("b", "drain")
        self.assign("a", "b")
        item = self.supervisor()
        item.step()
        self.record("a"), self.record("b")
        item.request_stop()
        item.step()
        until(lambda: self.lines("a", ".signals") and self.lines("b", ".signals"))
        # A second SIGTERM changes nothing.
        item.request_stop()
        self.assign("a", "b", "new")
        polls = len(self.backend.requests)
        self.after(item, 2)
        self.after(item, 2)
        self.assertIsNone(item.exit_code)
        self.assertEqual(len(self.backend.requests), polls + 2)
        self.assertFalse(any(body["last"] for body in self.backend.bodies))
        # The Supervisor starts no new worker process.
        self.assertEqual(self.starts("new"), 0)
        self.release("a"), self.release("b")
        self.step_until_exit(item, "a")
        self.step_until_exit(item, "b")
        item.step()
        self.assertEqual(item.exit_code, 0)
        self.assertEqual([self.lines(job, ".signals") for job in "ab"], [["SIGTERM"], ["SIGTERM"]])
        self.assertEqual([body["last"] for body in self.backend.bodies].count(True), 1)
        self.assertTrue(self.backend.bodies[-1]["last"])
        self.assertEqual(os.listdir(self.replays), [])
        self.assertEqual(self.events("exit")[0]["reason"], "sigterm")
        self.assertEqual(self.events("worker_exit")[0]["exit_code"], 143)

    def test_a_worker_that_does_not_drain_in_100_s_is_killed(self):
        self.plan("a", "ignore")
        self.assign("a")
        item = self.supervisor()
        item.step()
        pid = self.record("a")["pid"]
        item.request_stop()
        item.step()
        self.after(item, 99)
        self.assertIsNone(item.exit_code)
        self.assertTrue(alive(pid))
        self.after(item, 1)
        self.assertEqual(item.exit_code, 0)
        self.assertFalse(alive(pid))
        self.assertEqual(os.listdir(self.replays), [])
        self.assertTrue(self.backend.bodies[-1]["last"])

    def test_a_refused_poll_during_the_drain_ends_it_immediately(self):
        self.plan("a", "drain")
        self.assign("a")
        item = self.supervisor()
        item.step()
        pid = self.record("a")["pid"]
        item.request_stop()
        item.step()
        self.backend.reply = (401, {"detail": "refused"})
        self.after(item, 2)
        self.assertEqual(item.exit_code, 3)
        self.assertFalse(alive(pid))
        self.assertFalse(any(body["last"] for body in self.backend.bodies))

    def test_the_program_exits_with_code_0_after_a_real_sigterm(self):
        self.assign("a")
        runner = os.path.join(self.tmp, "runner.py")
        with open(runner, "w") as handle:
            handle.write(
                f"import sys\nsys.path.insert(0, {str(REPO / 'src')!r})\n"
                "from evalve.supervisor import supervisor\n"
                f"sys.exit(supervisor.main(**{self.options()!r}))\n"
            )
        environ = self.environ(EVALVE_POLL_INTERVAL_SECONDS="0.2")
        program = subprocess.Popen(
            [sys.executable, runner], env=environ, stdout=subprocess.PIPE, text=True
        )
        self.addCleanup(program.kill)
        self.record("a")
        program.send_signal(signal.SIGTERM)
        output, _ = program.communicate(timeout=30)
        self.assertEqual(program.returncode, 0)
        self.assertEqual(self.lines("a", ".signals"), ["SIGTERM"])
        self.assertTrue(self.backend.bodies[-1]["last"])
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(events[0]["event"], "start")
        self.assertEqual((events[-1]["event"], events[-1]["reason"]), ("exit", "sigterm"))


class EnvironmentTest(SupervisorCase):
    NAMES = (
        "EVALVE_BACKEND_URL", "EVALVE_LOOP_ID", "EVALVE_LOOP_TASK_SECRET", "KITARU_API_URL",
        "EVALVE_MAX_REPLAYS", "EVALVE_POLL_INTERVAL_SECONDS", "EVALVE_IDLE_TIMEOUT_SECONDS",
        "EVALVE_SOFT_LIFETIME_SECONDS",
    )  # fmt: skip

    def exit_of(self, **changes):
        out = io.StringIO()
        code = supervisor.main(self.environ(**changes), out=out, **self.options())
        return code, json.loads(out.getvalue())

    def test_a_missing_variable_gives_exit_code_2_and_a_log_line_with_its_name(self):
        for name in self.NAMES:
            with self.subTest(name):
                code, line = self.exit_of(**{name: None})
                self.assertEqual((code, line["variable"], line["event"]), (2, name, "exit"))
        self.assertEqual(self.backend.requests, [])

    def test_a_variable_that_is_not_valid_gives_exit_code_2(self):
        invalid = {
            "EVALVE_MAX_REPLAYS": ("0", "ten", "2.5"),
            "EVALVE_POLL_INTERVAL_SECONDS": ("0", "-1", "soon", "nan"),
            "EVALVE_IDLE_TIMEOUT_SECONDS": ("", "inf"),
            "EVALVE_SOFT_LIFETIME_SECONDS": ("x",),
            "EVALVE_LOOP_ID": ("  ",),
        }
        for name, values in invalid.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    code, line = self.exit_of(**{name: value})
                    self.assertEqual((code, line["variable"]), (2, name))


class LogTest(SupervisorCase):
    def test_no_log_line_holds_the_secret_or_a_worker_token(self):
        self.plan("a", "exit:0")
        self.assign("a", "b")
        item = self.supervisor()
        item.step()
        self.step_until_exit(item, "a")
        self.backend.reply = (500, {"detail": SECRET})
        self.after(item, 2)
        item.log("note", text=f"{SECRET} and token-of-b")
        self.backend.reply = (401, {"detail": "refused"})
        self.after(item, 2)
        output = self.out.getvalue()
        for hidden in (SECRET, "token-of-a", "token-of-b"):
            self.assertNotIn(hidden, output)
        self.assertEqual(self.events("note")[0]["text"], "*** and ***")
        # Each line is one JSON object with the time and the event.
        names = [event["event"] for event in self.events()]
        self.assertEqual(
            sorted(set(names)),
            ["exit", "note", "poll_failed", "poll_refused", "worker_exit", "worker_start"],
        )
        self.assertTrue(all(event["time"].startswith("2027-01-15T") for event in self.events()))

    def test_the_run_writes_the_start_and_the_exit(self):
        item = self.supervisor(EVALVE_IDLE_TIMEOUT_SECONDS="5")
        self.assertEqual(item.run(), 0)
        names = [event["event"] for event in self.events()]
        self.assertEqual((names[0], names[-1]), ("start", "exit"))
        self.assertEqual(self.events("start")[0]["contract_version"], 1)


class PackageTest(unittest.TestCase):
    def test_each_file_of_the_package_imports_only_the_standard_library(self):
        files = sorted(PACKAGE.glob("*.py"))
        self.assertIn(PACKAGE / "supervisor.py", files)
        for file in files:
            for node in ast.walk(ast.parse(file.read_text())):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    self.assertEqual(node.level, 0, f"{file.name}: a relative import")
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    with self.subTest(file=file.name, module=module):
                        self.assertIn(module.split(".")[0], sys.stdlib_module_names)

    def test_the_contract_version_is_1(self):
        self.assertEqual(supervisor.CONTRACT_VERSION, 1)

    def test_the_wheel_takes_the_package_from_src(self):
        with open(REPO / "pyproject.toml", "rb") as handle:
            wheel = tomllib.load(handle)["tool"]["hatch"]["build"]["targets"]["wheel"]
        self.assertEqual(wheel["packages"], ["src/evalve"])
        self.assertTrue((PACKAGE / "__init__.py").is_file())

    @unittest.skipUnless(shutil.which("uv"), "This test builds the wheel with uv.")
    def test_the_wheel_holds_the_supervisor(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(
                ["uv", "build", "--wheel", "--out-dir", directory, str(REPO)],
                check=True, capture_output=True,
            )  # fmt: skip
            (wheel,) = Path(directory).glob("*.whl")
            with zipfile.ZipFile(wheel) as archive:
                self.assertIn("evalve/supervisor/supervisor.py", archive.namelist())


if __name__ == "__main__":
    unittest.main()
