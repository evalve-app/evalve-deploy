"""Stand-in for the Supervisor of a Loop task (sc-498), standard library only.

It starts one Kitaru worker process for each assignment, as the Supervisor
does: a new session, a private directory that is HOME, TMPDIR and the working
directory, and the environment of the Supervisor story. It prints one JSON
object with the exit code and the times of each worker process.

Usage: launcher.py '<JSON spec>'
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = "/tmp/evalve"
LOGS = "/tmp/kcc"
COMMANDS = {
    "cli": ["/app/.venv/bin/kitaru", "worker", "start", "--timeout"],
    "module": ["/app/.venv/bin/python", "-m", "kitaru.worker"],
}


def main() -> int:
    spec = json.loads(sys.argv[1])
    mode = spec.get("mode", "cli")
    on_sigterm = spec.get("on_sigterm", "forward")
    os.makedirs(f"{ROOT}/replays", exist_ok=True)
    os.makedirs(f"{ROOT}/cache", exist_ok=True)
    os.makedirs(LOGS, exist_ok=True)
    procs: dict[str, subprocess.Popen] = {}
    info: dict[str, dict] = {}
    state = {"term": 0}

    def on_term(signum, _frame):
        state["term"] += 1
        with open(f"{LOGS}/launcher-signals.log", "a") as handle:
            handle.write(f"{time.time():.3f} {signal.Signals(signum).name}\n")
        if on_sigterm == "forward" and state["term"] == 1:
            for proc in procs.values():
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, on_term)

    for item in spec["assignments"]:
        job = item["job_id"]
        private = f"{ROOT}/replays/{job}"
        os.makedirs(private, mode=0o700, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != "EVALVE_LOOP_TASK_SECRET"}
        env.pop("KITARU_API_KEY", None)
        env.update(
            KITARU_WORKER_ID=item["worker_id"],
            KITARU_API_TOKEN=item["worker_token"],
            HOME=private,
            TMPDIR=private,
            UV_CACHE_DIR=f"{ROOT}/cache/uv",
            KITARU_WORKER_BLOB_CACHE_ROOT=f"{ROOT}/cache/kitaru-blobs",
            KITARU_WORKER_PAYLOAD_CACHE_ROOT=f"{ROOT}/cache/kitaru-payloads",
            KITARU_DISABLE_CLIENT_ANALYTICS="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        env.update(item.get("env", {}))
        timeout = str(item.get("timeout", 600))
        if mode == "cli":
            command = [*COMMANDS["cli"], timeout]
        else:
            command = COMMANDS["module"]
            env["KITARU_WORKER_TIMEOUT"] = timeout
        log = open(f"{LOGS}/worker-{job}.log", "ab")
        proc = subprocess.Popen(
            command,
            cwd=private,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        procs[job] = proc
        info[job] = {"pid": proc.pid, "started": time.time(), "private": private}
        with open(f"{LOGS}/pids.json", "w") as handle:
            json.dump({j: p.pid for j, p in procs.items()}, handle)

    while any("exit_code" not in info[j] for j in procs):
        for job, proc in procs.items():
            if "exit_code" in info[job]:
                continue
            code = proc.poll()
            if code is None:
                continue
            info[job]["exit_code"] = code
            info[job]["ended"] = time.time()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            if spec.get("cleanup", True):
                shutil.rmtree(info[job]["private"], ignore_errors=True)
        time.sleep(0.05)

    print(json.dumps({"workers": info, "sigterm_count": state["term"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
