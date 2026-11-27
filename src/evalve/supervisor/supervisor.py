"""The Supervisor: runs the Replays of a Loop task.

It polls the Evalve backend for its Assignments and runs one Kitaru worker
process for each Replay. In an agent image this file is
`/opt/evalve/supervisor.py`, and the system Python runs it. It imports only the
standard library, and nothing from `evalve` or `kitaru`.

The container must run a separate init process: enable
`linuxParameters.initProcessEnabled` in ECS, or use `docker run --init`.
Init reaps orphaned Replay descendants. The Supervisor waits for its direct
worker processes through Popen; it is not an implementation of PID 1.

The design is ADR 0006 point 13, parts "Claim", "Life" and "Isolation".
"""

import codecs
import errno
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONTRACT_VERSION = 1

WORKER_COMMAND = "/app/.venv/bin/kitaru"
# The only place of an agent image that uid 1000 can write.
ROOT = "/tmp/evalve"
# The container engine makes these directories, so the agent image cannot lock
# them. Code of a Replay can write them, and the sweep empties them.
RUNTIME_DIRECTORIES = ("/dev/shm", "/dev/mqueue")
PROC = "/proc"
# Each worker process gets its job ID in this variable, and each process of its
# Replay inherits it. Thus the Supervisor finds the processes of one Replay,
# also a process that left the session of the worker. A child that starts a
# new session with a cleared environment is outside this best-effort cleanup;
# the container-wide sweep still removes it before the next Run.
JOB_VARIABLE = "EVALVE_JOB_ID"

POLL_TIMEOUT_SECONDS = 10
# A worker process that still runs this long after the Run deadline is killed.
DEADLINE_GRACE_SECONDS = 30
# The longest drain after SIGTERM. It must stay below the `stopTimeout` of the
# task definition (120 s).
DRAIN_SECONDS = 100
# How long the Supervisor waits for a process that it killed.
REAP_SECONDS = 5
# How many times a kill looks for the processes of a Replay. A process can
# start a new one while the Supervisor reads `/proc`.
KILL_PASSES = 3
# A Run boundary must become quiet before another Run starts. Failure stops
# this Loop task rather than running an Experiment with leftover state.
SWEEP_SECONDS = 5
SWEEP_INTERVAL_SECONDS = 0.05
# Bound both memory use and time spent reading a noisy worker on each tick.
OUTPUT_CHUNK_BYTES = 4096
OUTPUT_READS_PER_TICK = 16
# The longest sleep between two looks at the worker processes.
TICK_SECONDS = 1

EXIT_CLEAN = 0
EXIT_CLEANUP = 1
EXIT_ENVIRONMENT = 2
EXIT_REFUSED = 3

REFUSED_STATUSES = (401, 409)


class SettingsError(Exception):
    """A variable of the environment is missing or is not valid."""

    def __init__(self, variable):
        super().__init__(variable)
        self.variable = variable


class Settings:
    """The environment of the Supervisor (contract version 1)."""

    def __init__(self, environ):
        self.backend_url = _url(environ, "EVALVE_BACKEND_URL").rstrip("/")
        self.loop_id = _text(environ, "EVALVE_LOOP_ID")
        if any(character.isspace() or character in "/?#" for character in self.loop_id):
            raise SettingsError("EVALVE_LOOP_ID")  # It is a part of the poll URL.
        self.secret = _text(environ, "EVALVE_LOOP_TASK_SECRET")
        self.kitaru_api_url = _url(environ, "KITARU_API_URL")
        self.max_replays = _number(environ, "EVALVE_MAX_REPLAYS", whole=True)
        self.poll_interval = _number(environ, "EVALVE_POLL_INTERVAL_SECONDS")
        self.idle_timeout = _number(environ, "EVALVE_IDLE_TIMEOUT_SECONDS")
        self.soft_lifetime = _number(environ, "EVALVE_SOFT_LIFETIME_SECONDS")


def _text(environ, name):
    value = (environ.get(name) or "").strip()
    if not value:
        raise SettingsError(name)
    return value


def _url(environ, name):
    text = _text(environ, name)
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        raise SettingsError(name) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SettingsError(name)
    if parts.scheme == "http" and parts.hostname != "localhost":
        try:
            loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise SettingsError(name)
    return text


def _number(environ, name, whole=False):
    text = _text(environ, name)
    try:
        value = int(text) if whole else float(text)
    except ValueError:
        raise SettingsError(name) from None
    if not math.isfinite(value) or value <= 0:
        raise SettingsError(name)
    return value


class Clock:
    """The time. A test replaces it."""

    def now(self):
        return time.time()

    def sleep(self, seconds):
        time.sleep(seconds)


class Assignment:
    """What the backend gives the Loop task so that it can run one Replay."""

    def __init__(self, item):
        self.run_id = _field(item, "run_id")
        self.job_id = _field(item, "job_id")
        self.worker_id = _field(item, "worker_id")
        self.worker_token = _field(item, "worker_token")
        # The job ID is the name of the private directory.
        if "/" in self.job_id or self.job_id in (".", ".."):
            raise ValueError("job_id")


def _field(item, name):
    value = item[name]
    # These strings become paths and process environment values. Reject NUL,
    # other controls and lone surrogates while a bad reply is still a failed poll.
    if not isinstance(value, str) or not value or not value.isprintable():
        raise ValueError(name)
    return value


class Reply:
    """A good reply to a poll."""

    def __init__(self, body):
        deadline = body["run_deadline"]
        self.run_deadline = None if deadline is None else _timestamp(deadline)
        self.assignments = [Assignment(item) for item in body["assignments"]]


def _timestamp(text):
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class Refused(Exception):
    """The backend refused the poll (401 or 409)."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


class PollFailed(Exception):
    """Each other error of a poll. It changes nothing."""


class PollRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep credentials on the configured endpoint, including across schemes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # A redirect is a failed poll, without another request.


def poll(settings, last, timeout=POLL_TIMEOUT_SECONDS):
    """Send one poll. Return a Reply, or raise Refused or PollFailed."""
    try:
        request = urllib.request.Request(
            f"{settings.backend_url}/v1/loops/{settings.loop_id}/task/poll",
            data=json.dumps({"contract_version": CONTRACT_VERSION, "last": last}).encode(),
            headers={
                "Authorization": f"EvalveLoopTask {settings.secret}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        opener = urllib.request.build_opener(PollRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            return Reply(json.loads(response.read()))
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in REFUSED_STATUSES:
            raise Refused(error.code) from None
        raise PollFailed(f"status {error.code}") from None
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as error:
        # A timeout, a network error, or a reply that the Supervisor cannot read.
        raise PollFailed(f"{type(error).__name__}: {error}") from None


def read_processes(proc_path, *, strict=False):
    """Return {pid: (parent pid, session)} for each process in `proc_path`.

    It reads only the `stat` files. It reads no `cmdline` file: in a local run
    of an agent image on an Apple Silicon Mac, that read can stop the program
    with exit code 133.
    """
    table = {}
    try:
        names = os.listdir(proc_path)
    except OSError:
        if strict:
            raise
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_path, name, "stat")) as handle:
                text = handle.read()
            # The name of the program is in brackets and can hold a space or a bracket.
            fields = text[text.rindex(")") + 1 :].split()
            if fields[0] in ("Z", "X"):
                continue  # A dead process cannot fork or write; init reaps it.
            table[int(name)] = (int(fields[1]), int(fields[3]))
        except (FileNotFoundError, ProcessLookupError):
            continue  # The process ended while the Supervisor read it.
        except (OSError, ValueError, IndexError):
            if strict:
                raise
            continue  # The process ended while the Supervisor read it.
    return table


def processes_to_sweep(proc_path, own_pid):
    """Select each process apart from `own_pid` and each process above it."""
    table = read_processes(proc_path, strict=True)
    keep = {1, own_pid}
    pid = own_pid
    while pid in table:
        pid = table[pid][0]
        if pid in keep:
            break
        keep.add(pid)
    return sorted(pid for pid in table if pid not in keep)


def processes_of_session(proc_path, session):
    """Select each process of one session."""
    table = read_processes(proc_path)
    return sorted(pid for pid, (_, its_session) in table.items() if its_session == session)


def processes_with_variable(proc_path, name, value):
    """Select each process that has this variable with this value in its environment.

    It reads the `environ` files. It cannot read the file of a process of a
    different user, and it leaves out that process.
    """
    entry = f"{name}={value}".encode()
    found = []
    try:
        names = os.listdir(proc_path)
    except OSError:
        return found
    for item in names:
        if not item.isdigit():
            continue
        try:
            with open(os.path.join(proc_path, item, "environ"), "rb") as handle:
                variables = handle.read().split(b"\0")
        except OSError:
            continue
        if entry in variables:
            found.append(int(item))
    return sorted(found)


def send_kill(pids):
    """Send SIGKILL to each process. Return the number of signals that it sent."""
    sent = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            sent += 1
        except (ProcessLookupError, PermissionError):
            pass
    return sent


class Worker:
    """One Kitaru worker process, for one Replay."""

    def __init__(self, assignment, process, directory, started, deadline):
        self.job_id = assignment.job_id
        self.process = process
        self.directory = directory
        self.started = started
        self.deadline = deadline
        self.killed = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.output = ""


class Supervisor:
    def __init__(
        self,
        settings,
        environ,
        *,
        clock=None,
        out=None,
        worker_command=WORKER_COMMAND,
        root=ROOT,
        runtime_directories=RUNTIME_DIRECTORIES,
        proc_path=PROC,
        poll_timeout=POLL_TIMEOUT_SECONDS,
    ):
        self.settings = settings
        self.environ = dict(environ)
        self.clock = clock or Clock()
        self.out = out or sys.stdout
        self.worker_command = worker_command
        self.root = root
        self.replays = os.path.join(root, "replays")
        self.runtime_directories = runtime_directories
        self.proc_path = proc_path
        self.poll_timeout = poll_timeout

        self.started_at = self.clock.now()
        self.next_poll_at = self.started_at  # The first poll is immediate.
        self.idle_since = self.started_at  # The idle clock starts with the Supervisor.
        self.workers = {}  # job ID -> the worker process that runs
        self.started_jobs = set()  # each job ID that had its start in this Loop task
        self.assignments = []  # the Assignments of the last good reply
        self.run_deadline = None
        self.run_id = None  # the Run ID of the Assignments that the Supervisor saw last
        self.tokens = set()
        self.stop_requested_at = None  # the time of the first SIGTERM
        self.draining = False
        self.exit_code = None

    # ---------------------------------------------------------------- the log

    def log(self, event, **fields):
        moment = datetime.fromtimestamp(self.clock.now(), timezone.utc).isoformat()
        line = json.dumps({"time": moment, "event": event, **fields}, default=str)
        # No line holds the channel secret or a worker token.
        for hidden in (self.settings.secret, *self.tokens):
            line = line.replace(json.dumps(hidden)[1:-1], "***")
        print(line, file=self.out, flush=True)

    # --------------------------------------------------------------- the loop

    def run(self):
        self.log(
            "start",
            contract_version=CONTRACT_VERSION,
            loop_id=self.settings.loop_id,
            max_replays=self.settings.max_replays,
        )
        while True:
            self.step()
            if self.exit_code is not None:
                return self.exit_code
            wait = self.next_poll_at - self.clock.now()
            self.clock.sleep(min(TICK_SECONDS, max(0.05, wait)))

    def request_stop(self):
        """SIGTERM. A second SIGTERM changes nothing."""
        if self.stop_requested_at is None:
            self.stop_requested_at = self.clock.now()

    def _drain_end(self):
        """The drain ends this long after the SIGTERM, also when a poll was slow."""
        return self.stop_requested_at + DRAIN_SECONDS

    def step(self):
        """Look at the worker processes, poll when it is time, and decide on an exit."""
        self._collect_exits()
        self._kill_after_deadline()
        if self.stop_requested_at is not None and not self.draining:
            self._start_drain()
        if self.clock.now() >= self.next_poll_at and self._time_for_a_poll():
            self._poll()
            self.next_poll_at = self.clock.now() + self.settings.poll_interval
        if self.exit_code is not None:
            return
        if self.stop_requested_at is not None:
            # A SIGTERM during the poll starts its drain in the next step.
            if self.draining:
                self._continue_drain()
        else:
            self._exit_when_idle()

    def _time_for_a_poll(self):
        """During the drain, no poll starts that can end after the end of the drain."""
        if self.stop_requested_at is None:
            return True
        return self.clock.now() + self.poll_timeout <= self._drain_end()

    def _poll(self):
        try:
            reply = poll(self.settings, last=False, timeout=self.poll_timeout)
        except Refused as refused:
            self._refused(refused.status)
            return
        except PollFailed as failure:
            # The workers continue, the clocks continue, and the next poll follows.
            self.log("poll_failed", error=str(failure))
            return
        self.assignments = reply.assignments
        self.run_deadline = reply.run_deadline
        self.tokens.update(item.worker_token for item in reply.assignments)
        if self.stop_requested_at is not None:
            # After SIGTERM the Supervisor starts no new worker process. The
            # signal can arrive while the poll waits for its reply.
            return
        if reply.assignments:
            run_id = reply.assignments[0].run_id
            if self.run_id is not None and run_id != self.run_id:
                if not self._sweep():
                    return
            if self.stop_requested_at is not None:
                return  # SIGTERM can also arrive during the sweep.
            self.run_id = run_id
        self._start_workers()

    def _refused(self, status):
        self.log("poll_refused", status=status)
        self._kill_workers()
        self._delete_private_directories()
        self._exit(EXIT_REFUSED, "refused poll")

    # ------------------------------------------------------------ the workers

    def _start_workers(self):
        now = self.clock.now()
        if self.run_deadline is None or self.run_deadline <= now:
            return  # With no deadline, or one in the past, no worker starts.
        seconds = math.ceil(self.run_deadline - now)
        for assignment in self.assignments:
            if assignment.job_id in self.started_jobs:
                continue  # At most one worker process for each job ID.
            if len(self.workers) >= self.settings.max_replays:
                break  # The others wait for a later poll.
            if not self._start_worker(assignment, seconds):
                break  # The start is tried again at the next poll.

    def _start_worker(self, assignment, seconds):
        directory = os.path.join(self.replays, assignment.job_id)
        try:
            os.makedirs(os.path.join(self.root, "cache"), exist_ok=True)
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            process = subprocess.Popen(
                [self.worker_command, "worker", "start", "--timeout", str(seconds)],
                cwd=directory,
                env=self._worker_environment(assignment, directory),
                stdin=subprocess.DEVNULL,
                # Capture stdout and stderr so ordinary worker output is JSON
                # data in worker_output events in the merged container log.
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Thus only the Supervisor sends a signal to a worker.
                start_new_session=True,
            )
        except OSError as error:
            # This does not count as a start.
            full = error.errno == errno.ENOSPC
            self.log(
                "disk_full" if full else "worker_start_failed",
                job_id=assignment.job_id,
                error=f"{type(error).__name__}: {error}",
            )
            self._delete(directory)
            return False
        now = self.clock.now()
        self.started_jobs.add(assignment.job_id)
        self.workers[assignment.job_id] = Worker(
            assignment, process, directory, now, self.run_deadline
        )
        os.set_blocking(process.stdout.fileno(), False)
        self.log(
            "worker_start",
            job_id=assignment.job_id,
            run_id=assignment.run_id,
            worker_id=assignment.worker_id,
            pid=process.pid,
            timeout=seconds,
        )
        return True

    def _worker_environment(self, assignment, directory):
        # Agent code can still read the secret from `/proc`. That is a documented limit.
        hidden = ("EVALVE_LOOP_TASK_SECRET", "KITARU_API_KEY")
        env = {name: value for name, value in self.environ.items() if name not in hidden}
        cache = os.path.join(self.root, "cache")
        env.update(
            KITARU_API_URL=self.settings.kitaru_api_url,
            KITARU_WORKER_ID=assignment.worker_id,
            KITARU_API_TOKEN=assignment.worker_token,
            **{JOB_VARIABLE: assignment.job_id},
            HOME=directory,
            TMPDIR=directory,
            UV_CACHE_DIR=os.path.join(cache, "uv"),
            KITARU_WORKER_BLOB_CACHE_ROOT=os.path.join(cache, "kitaru-blobs"),
            KITARU_WORKER_PAYLOAD_CACHE_ROOT=os.path.join(cache, "kitaru-payloads"),
            KITARU_DISABLE_CLIENT_ANALYTICS="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        return env

    def _collect_exits(self):
        for worker in list(self.workers.values()):
            self._read_worker_output(worker)
            code = worker.process.poll()
            if code is None:
                continue
            self._kill_replay(worker)  # each process of the Replay that is left
            self._read_worker_output(worker, final=True)
            self._delete(worker.directory)
            del self.workers[worker.job_id]
            self.log(
                "worker_exit",
                job_id=worker.job_id,
                exit_code=code,
                seconds=round(self.clock.now() - worker.started, 1),
            )

    def _read_worker_output(self, worker, *, final=False):
        pipe = worker.process.stdout
        if pipe.closed:
            return
        eof = False
        for _ in range(OUTPUT_READS_PER_TICK):
            try:
                data = os.read(pipe.fileno(), OUTPUT_CHUNK_BYTES)
            except BlockingIOError:
                break
            if not data:
                eof = True
                break
            worker.output += worker.decoder.decode(data)
            self._log_worker_output(worker)
        if eof or final:
            if final and not eof:
                try:
                    if os.read(pipe.fileno(), 1):
                        self.log("worker_output_truncated", job_id=worker.job_id)
                except BlockingIOError:
                    pass
            worker.output += worker.decoder.decode(b"", final=True)
            self._log_worker_output(worker, final=True)
            # A detached process may still hold the pipe. Never wait for it.
            pipe.close()

    def _log_worker_output(self, worker, *, final=False):
        # Retain a suffix so a credential split across reads is still redacted.
        secrets = (self.settings.secret, *self.tokens)
        pattern = re.compile("|".join(
            re.escape(value) for value in sorted(secrets, key=len, reverse=True)
        ))
        while worker.output:
            length = len(worker.output)
            limit = length if final else max(0, length - max(map(len, secrets)) + 1)
            limit = min(limit, OUTPUT_CHUNK_BYTES)
            if not limit:
                break
            # Consume a complete match at the boundary, then redact it before
            # writing. Even a credential longer than a chunk stays bounded.
            for match in pattern.finditer(worker.output):
                if match.start() >= limit:
                    break
                limit = max(limit, match.end())
            text = pattern.sub("***", worker.output[:limit])
            self.log("worker_output", job_id=worker.job_id, text=text)
            worker.output = worker.output[limit:]
            if not final:
                break

    def _kill_after_deadline(self):
        now = self.clock.now()
        for worker in self.workers.values():
            if not worker.killed and now >= worker.deadline + DEADLINE_GRACE_SECONDS:
                self.log("worker_deadline", job_id=worker.job_id)
                self._kill_replay(worker)

    def _kill_replay(self, worker):
        """Send SIGKILL to each process of the Replay of a worker process.

        Kitaru starts the agent in its own session, so the session of the worker
        is not sufficient. A process with no job ID in its environment stays
        until the sweep.
        """
        worker.killed = True
        pid = worker.process.pid
        for _ in range(KILL_PASSES):
            found = set(processes_with_variable(self.proc_path, JOB_VARIABLE, worker.job_id))
            found.update(processes_of_session(self.proc_path, pid))
            found.discard(os.getpid())
            if not send_kill(sorted(found)):
                break
        try:
            # The worker process leads its session and its process group.
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _kill_workers(self):
        """Send SIGKILL to each worker process, and wait for its end."""
        for worker in self.workers.values():
            self._kill_replay(worker)
        for worker in self.workers.values():
            try:
                worker.process.wait(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        self._collect_exits()

    # -------------------------------------------------------------- the sweep

    def _sweep(self):
        """Before the first worker of a new Run: leave nothing of the Run before it."""
        self._kill_workers()
        end = self.clock.now() + SWEEP_SECONDS
        killed = set()
        quiet = 0
        try:
            while quiet < 2:
                processes = processes_to_sweep(self.proc_path, os.getpid())
                quiet = 0 if processes else quiet + 1
                if processes:
                    send_kill(processes)
                    killed.update(processes)
                if quiet == 2:
                    break
                if self.clock.now() >= end:
                    raise OSError("processes remain after sweep deadline")
                self.clock.sleep(SWEEP_INTERVAL_SECONDS)
            entries = self._empty(self.replays, strict=True)
            for directory in self.runtime_directories:
                entries += self._empty(directory, strict=True)
        except (OSError, ValueError, IndexError) as error:
            self.log("sweep_failed", error=f"{type(error).__name__}: {error}")
            self._exit(EXIT_CLEANUP, "cleanup failed")
            return False
        self.log("sweep", processes=len(killed), entries=entries)
        return True

    def _delete_private_directories(self):
        self._empty(self.replays)

    def _empty(self, directory, *, strict=False):
        """Delete each entry of a directory. Return the number of deleted entries."""
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            return 0  # The directory does not exist here.
        except OSError:
            if strict:
                raise
            return 0
        deleted = 0
        for entry in entries:
            if self._delete(entry.path):
                deleted += 1
            elif strict and os.path.lexists(entry.path):
                raise OSError(f"could not delete {entry.path}")
        return deleted

    def _delete(self, path):
        """Delete a file or a directory. A delete that fails gets a log entry."""
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as error:
            self.log("delete_failed", path=path, error=f"{type(error).__name__}: {error}")
            return False
        return True

    # ---------------------------------------------------------- idle and exit

    def _idle(self):
        """No worker process runs, and each Assignment of the last good reply had its start."""
        return not self.workers and all(
            assignment.job_id in self.started_jobs for assignment in self.assignments
        )

    def _exit_when_idle(self):
        now = self.clock.now()
        if not self._idle():
            self.idle_since = None
            return
        if self.idle_since is None:
            self.idle_since = now
        if now - self.started_at >= self.settings.soft_lifetime:
            # The soft limit does not wait for the idle timeout.
            self._clean_exit("soft lifetime limit")
        elif now - self.idle_since >= self.settings.idle_timeout:
            self._clean_exit("idle")

    def _start_drain(self):
        self.draining = True
        self.log("sigterm", workers=len(self.workers))
        for worker in self.workers.values():
            try:
                # One SIGTERM: the worker drains. A second one cancels its tasks.
                worker.process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _continue_drain(self):
        if self.workers and self.clock.now() < self._drain_end():
            return
        self._kill_workers()
        self._delete_private_directories()
        self._clean_exit("sigterm")

    def _clean_exit(self, reason):
        try:
            # The last poll. Its result does not count.
            poll(self.settings, last=True, timeout=self.poll_timeout)
        except (Refused, PollFailed):
            pass
        self._exit(EXIT_CLEAN, reason)

    def _exit(self, code, reason):
        self.exit_code = code
        self.log("exit", reason=reason, code=code)


def main(environ=None, **options):
    """Run the Supervisor until its exit. Return the exit code."""
    environ = os.environ if environ is None else environ
    try:
        settings = Settings(environ)
    except SettingsError as error:
        moment = datetime.now(timezone.utc).isoformat()
        line = {"time": moment, "event": "exit", "reason": "environment",
                "variable": error.variable, "code": EXIT_ENVIRONMENT}  # fmt: skip
        print(json.dumps(line), file=options.get("out") or sys.stdout, flush=True)
        return EXIT_ENVIRONMENT
    supervisor = Supervisor(settings, environ, **options)
    signal.signal(signal.SIGTERM, lambda signum, frame: supervisor.request_stop())
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
