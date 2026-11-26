"""Probe agent for the Kitaru contract check.

It runs as the agent task of a Replay. It records what agent code can see and
write, in the outputs of the session that it creates. It calls no model.
"""

import asyncio
import errno
import glob
import hashlib
import json
import os
import signal
import sys
import time
import uuid
from datetime import UTC, datetime

from kitaru.api_models.v1.session import (
    SessionCreateRequest,
    SessionOrigin,
    SessionStatus,
    SessionUpdateRequest,
)
from kitaru.client.api_client import KitaruAPIClient
from kitaru.task import get_task_id, get_task_inputs

LOGS = "/tmp/kcc"
SIGNAL_LOG = f"{LOGS}/agent-signals-{os.getpid()}.log"


def sha(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).hexdigest()


def log_signal(signum, _frame) -> None:
    with open(SIGNAL_LOG, "a") as handle:
        handle.write(f"{time.time():.3f} {signal.Signals(signum).name}\n")


def read_ancestors() -> list[dict]:
    """Read the environment of each process above this one from /proc."""
    found = []
    pid = os.getppid()
    for _ in range(8):
        if pid <= 0:
            break
        entry: dict = {"pid": pid}
        try:
            with open(f"/proc/{pid}/comm") as handle:
                entry["comm"] = handle.read().strip()
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                entry["cmdline"] = handle.read().replace(b"\0", b" ").decode().strip()
            with open(f"/proc/{pid}/environ", "rb") as handle:
                pairs = dict(
                    item.split(b"=", 1)
                    for item in handle.read().split(b"\0")
                    if b"=" in item
                )
            entry["environ_readable"] = True
            entry["worker_id"] = (pairs.get(b"KITARU_WORKER_ID") or b"").decode() or None
            entry["api_token_sha256"] = sha(pairs.get(b"KITARU_API_TOKEN"))
            entry["api_key_present"] = b"KITARU_API_KEY" in pairs
            entry["loop_task_secret_sha256"] = sha(pairs.get(b"EVALVE_LOOP_TASK_SECRET"))
        except OSError as error:
            entry["environ_readable"] = False
            entry["error"] = errno.errorcode.get(error.errno, str(error))
        found.append(entry)
        try:
            with open(f"/proc/{pid}/stat") as handle:
                pid = int(handle.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return found


def try_write(path: str | None, marker: str) -> dict:
    """Try to create a marker file, and list the markers of earlier Replays."""
    if not path:
        return {"path": path, "write": "not set"}
    result: dict = {"path": path}
    try:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, marker), "w") as handle:
            handle.write("x")
        result["write"] = "ok"
    except OSError as error:
        result["write"] = errno.errorcode.get(error.errno, str(error))
    result["markers_of_other_replays"] = sorted(
        os.path.basename(item)
        for item in glob.glob(os.path.join(path, "kcc-marker-*"))
        if os.path.basename(item) != marker
    )
    return result


def probe(marker: str) -> dict:
    env = os.environ
    replays = "/tmp/evalve/replays"
    siblings = sorted(os.listdir(replays)) if os.path.isdir(replays) else []
    return {
        "uid": os.getuid(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "home": env.get("HOME"),
        "tmpdir": env.get("TMPDIR"),
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "env_names": sorted(env),
        "plain_env": env.get("KCC_PLAIN"),
        "secret_sha256": sha(env.get("KCC_SECRET_VALUE")),
        "has_kitaru_api_key": "KITARU_API_KEY" in env,
        "task_token_sha256": sha(env.get("KITARU_API_TOKEN")),
        "has_worker_id": "KITARU_WORKER_ID" in env,
        "has_loop_task_secret": "EVALVE_LOOP_TASK_SECRET" in env,
        "ancestors": read_ancestors(),
        "replay_dirs_visible": siblings,
        "writes": [
            try_write(path, marker)
            for path in (
                "/app",
                "/app/agent",
                "/app/.venv",
                "/app/.venv/lib/python3.13/site-packages",
                "/opt/kcc",
                env.get("HOME"),
                os.getcwd(),
                "/tmp",
                "/var/tmp",
                "/dev/shm",
                "/home/kitaru",
                env.get("UV_CACHE_DIR"),
                env.get("KITARU_WORKER_BLOB_CACHE_ROOT"),
                env.get("KITARU_WORKER_PAYLOAD_CACHE_ROOT"),
            )
        ],
    }


async def main() -> int:
    os.makedirs(LOGS, exist_ok=True)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, log_signal)
    if get_task_id() is None:
        raise RuntimeError("No task id in the environment")
    inputs = get_task_inputs() or {}
    replay_id = os.environ.get("KITARU_REPLAY_ID")
    sleep = float(os.environ.get("KCC_SLEEP_OVERRIDE") or inputs.get("sleep", 0))
    marker = f"kcc-marker-{replay_id or uuid.uuid4()}"
    report = probe(marker)
    report["started_at"] = time.time()
    async with KitaruAPIClient() as client:
        session = await client.sessions.create(
            SessionCreateRequest(
                agent_id=uuid.UUID(os.environ["KCC_AGENT_ID"]),
                origin=SessionOrigin.REPLAY if replay_id else SessionOrigin.RECORDED,
                status=SessionStatus.IN_PROGRESS,
                name=os.environ.get("KITARU_SESSION_NAME"),
                inputs=inputs,
                outputs=None,
                started_at=datetime.now(UTC),
                framework="kcc-probe",
                adapter_version="1",
            )
        )
        deadline = time.monotonic() + sleep
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        report["ended_at"] = time.time()
        report["signals"] = (
            open(SIGNAL_LOG).read().split("\n") if os.path.exists(SIGNAL_LOG) else []
        )
        await client.sessions.update(
            session.id,
            SessionUpdateRequest(
                status=SessionStatus.COMPLETED,
                outputs=report,
                ended_at=datetime.now(UTC),
            ),
        )
    print(f"probe agent recorded session {session.id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
