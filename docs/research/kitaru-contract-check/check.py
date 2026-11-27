# /// script
# requires-python = ">=3.12"
# dependencies = ["kitaru==0.27.6"]
# ///
"""Kitaru contract check for a Loop task (Shortcut story sc-465).

It asserts the Kitaru facts that the design of the Loop task reads from the
Kitaru source. It starts a local Kitaru server and Kitaru workers in
containers. It needs no AWS.

Run it before each Kitaru upgrade:

    uv run check.py                 # all checks, about 15 minutes
    uv run check.py --only token    # the checks whose name contains "token"
    uv run check.py --keep          # leave the containers for inspection
    uv run check.py --lock          # with the block of sc-534 (see below)

To check a different Kitaru version, change the version in the `dependencies`
line above. The server image, the worker image and the client follow it.

With `--lock`, the test agent image has the block of the story sc-534: uid 1000
can write only below `/tmp/evalve`. The checks are the same, plus the facts
that hold only with the block. A full run with `--lock` writes
`results-lock.json`.

It needs `uv`, and `docker` or `podman`. It pulls images from Docker Hub, and
an evaluator installs `openai` from PyPI. It writes `results.json` next to
this file. A check is PASS, FAIL or INFO. INFO is a check that records what
Kitaru does and has no expected result.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ["KITARU_DISABLE_CLIENT_ANALYTICS"] = "1"

from kitaru.api_models.v1.agent import AgentCreateRequest  # noqa: E402
from kitaru.api_models.v1.agent_version import (  # noqa: E402
    AgentVersionCreateRequest,
    RunSpec,
)
from kitaru.api_models.v1.api_key import ApiKeyCreateRequest  # noqa: E402
from kitaru.api_models.v1.cohort import CohortCreateRequest  # noqa: E402
from kitaru.api_models.v1.cohort_version import (  # noqa: E402
    CohortVersionCreateRequest,
)
from kitaru.api_models.v1.connection import ConnectionCreateRequest  # noqa: E402
from kitaru.api_models.v1.evaluator import (  # noqa: E402
    EvaluatorCreateRequest,
    EvaluatorVersionCreateRequest,
)
from kitaru.api_models.v1.experiment import ExperimentCreateRequest  # noqa: E402
from kitaru.api_models.v1.experiment_run import (  # noqa: E402
    ExperimentRunCreateRequest,
)
from kitaru.api_models.v1.plugin import (  # noqa: E402
    EvaluatorConfig,
    ScriptPluginSource,
)
from kitaru.api_models.v1.replay import BaselineEvaluationMode  # noqa: E402
from kitaru.api_models.v1.secret import SecretCreateRequest  # noqa: E402
from kitaru.api_models.v1.session import (  # noqa: E402
    SessionCreateRequest,
    SessionOrigin,
    SessionStatus,
)
from kitaru.api_models.v1.worker import (  # noqa: E402
    WorkerClaim,
    WorkerCreateRequest,
    WorkerRuntime,
    WorkerScope,
)
from kitaru.client.api_client import KitaruAPIClient  # noqa: E402
from kitaru.client.exceptions import APIError  # noqa: E402

HERE = Path(__file__).resolve().parent
VERSION = importlib.metadata.version("kitaru")
SERVER_IMAGE = f"docker.io/zenmldocker/kitaru-server:{VERSION}"
WORKER_IMAGE = f"docker.io/zenmldocker/kitaru-worker:{VERSION}"
AGENT_IMAGE = f"localhost/kcc-agent:{VERSION}"
POSTGRES_IMAGE = "docker.io/library/postgres:16-alpine"
ENGINE = "docker" if shutil.which("docker") else "podman"
LOCK = False  # `--lock`: build the test agent image with the block of sc-534.
BLOCKED = ("/tmp", "/var/tmp", "/run/lock", "/home/kitaru")
# Check 6 of `evalve build`: the places of the image that uid 1000 can write.
FIND_WRITABLE = (
    "find", "/", "-xdev",
    "(", "-path", "/proc", "-o", "-path", "/sys", "-o", "-path", "/dev", "-o", "-path", "/tmp/evalve", ")",
    "-prune", "-o", "-writable", "-print",
)  # fmt: skip
PASSWORD = "kcc-password"
SHORT_TOKEN_SECONDS = 40
SETTLED = {"completed", "failed", "canceled"}


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def engine(*args: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    """Run one container engine command."""
    result = subprocess.run(
        [ENGINE, *args], capture_output=True, text=True, timeout=timeout
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{ENGINE} {' '.join(args)}\n{result.stdout}{result.stderr}")
    return result


async def engine_async(*args: str) -> tuple[int, str, str]:
    """Run one container engine command and do not block the event loop."""
    proc = await asyncio.create_subprocess_exec(
        ENGINE,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(), err.decode()


class Recorder:
    """The results of the checks."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, check: str, fact: str, status: str, **detail: Any) -> None:
        self.items.append(
            {"check": check, "fact": fact, "status": status, "detail": detail}
        )
        print(f"  [{status}] {fact}")
        for key, value in detail.items():
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            print(f"         {key}: {text[:600]}")

    def expect(self, check: str, fact: str, ok: bool, **detail: Any) -> None:
        self.add(check, fact, "PASS" if ok else "FAIL", **detail)

    def info(self, check: str, fact: str, **detail: Any) -> None:
        self.add(check, fact, "INFO", **detail)


@dataclass
class Stack:
    """The containers of one run of the check."""

    run_id: str
    url: str = ""
    short_url: str = ""
    boxes: list[str] = field(default_factory=list)

    @property
    def network(self) -> str:
        return f"{self.run_id}-net"

    @property
    def inner_url(self) -> str:
        return f"http://{self.run_id}-server:8000"

    def up(self) -> None:
        engine("network", "create", self.network)
        engine(
            "run", "-d", "--name", f"{self.run_id}-db", "--network", self.network,
            "-e", "POSTGRES_PASSWORD=kcc", "-e", "POSTGRES_DB=kitaru", POSTGRES_IMAGE,
        )  # fmt: skip
        for _ in range(60):
            ready = engine(
                "exec", f"{self.run_id}-db", "pg_isready", "-U", "postgres", "-d", "kitaru",
                check=False,
            )  # fmt: skip
            if ready.returncode == 0:
                break
            time.sleep(1)
        self.url = self._server("server", {})
        self.short_url = self._server(
            "server-short",
            {
                "KITARU_SERVER_WORKER_TOKEN_LIFETIME_SECONDS": str(SHORT_TOKEN_SECONDS),
                "KITARU_SERVER_SKIP_DB_MIGRATION": "true",
            },
        )

    def _server(self, name: str, extra: dict[str, str]) -> str:
        """Start one server on the shared database and wait for its health."""
        env = {
            "KITARU_SERVER_DB_HOST": f"{self.run_id}-db",
            "KITARU_SERVER_DB_USER": "postgres",
            "KITARU_SERVER_DB_PWD": "kcc",
            "KITARU_SERVER_DB_NAME": "kitaru",
            "KITARU_SERVER_AUTH_SCHEME": "local",
            "KITARU_SERVER_JWT_SIGNING_KEY": "kcc-signing-key-for-a-local-test-only",
            "KITARU_SERVER_SECRET_ENCRYPTION_KEY": "kcc-encryption-key-local-test",
            "KITARU_SERVER_DEFAULT_ACCOUNT_PASSWORD": PASSWORD,
            "KITARU_SERVER_ANALYTICS_OPT_IN": "false",
            **extra,
        }
        args = ["run", "-d", "--name", f"{self.run_id}-{name}", "--network", self.network,
                "-p", "127.0.0.1::8000"]  # fmt: skip
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        engine(*args, SERVER_IMAGE)
        port = engine("port", f"{self.run_id}-{name}", "8000").stdout.strip().rsplit(":", 1)[1]
        url = f"http://127.0.0.1:{port}"
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
                    if response.status == 200:
                        return url
            except OSError:
                pass
            time.sleep(1)
        logs = engine("logs", "--tail", "40", f"{self.run_id}-{name}", check=False)
        raise RuntimeError(f"Server {name} is not healthy\n{logs.stdout}{logs.stderr}")

    def box(self, name: str, command: list[str] | None = None, env: dict[str, str] | None = None) -> str:
        """Start one container from the test agent image, as a Loop task."""
        full = f"{self.run_id}-{name}"
        args = ["run", "-d", "--init", "--name", full, "--network", self.network,
                "-e", f"KITARU_API_URL={self.inner_url}",
                "-e", "EVALVE_LOOP_TASK_SECRET=kcc-channel-secret"]  # fmt: skip
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        engine(*args, AGENT_IMAGE, *(command or ["sleep", "infinity"]))
        self.boxes.append(full)
        return full

    def down(self) -> None:
        names = [*self.boxes, f"{self.run_id}-server-short", f"{self.run_id}-server", f"{self.run_id}-db"]
        # `-t` is an option of podman only. `docker rm -f` does not wait.
        now = ["-t", "0"] if ENGINE == "podman" else []
        engine("rm", "-f", *now, *names, check=False)
        engine("network", "rm", self.network, check=False)


@dataclass
class Ctx:
    """What the checks share."""

    stack: Stack
    client: KitaruAPIClient
    api_key: str
    rec: Recorder
    agent_id: uuid.UUID = uuid.UUID(int=0)
    agent_version_id: uuid.UUID = uuid.UUID(int=0)
    experiment_id: uuid.UUID = uuid.UUID(int=0)
    secret_value: str = ""
    openai_key: str = ""
    cache: dict[str, Any] = field(default_factory=dict)


def build_images() -> None:
    for image in (POSTGRES_IMAGE, SERVER_IMAGE, WORKER_IMAGE):
        # `image exists` is a command of podman only.
        present = "exists" if ENGINE == "podman" else "inspect"
        if engine("image", present, image, check=False).returncode != 0:
            print(f"Pulling {image} ...")
            engine("pull", image, timeout=1800)
    print(f"Building {AGENT_IMAGE} ...")
    engine(
        "build", "-q", "--build-arg", f"KITARU_VERSION={VERSION}",
        "--build-arg", f"LOCK={int(LOCK)}", "-t", AGENT_IMAGE,
        str(HERE / "image"), timeout=1800,
    )  # fmt: skip


async def bootstrap_key(url: str) -> str:
    async with KitaruAPIClient(base_url=url) as anonymous:
        token = await anonymous.auth.login(username="default", password=PASSWORD)
    async with KitaruAPIClient(base_url=url, api_key=token.access_token) as session:
        issued = await session.api_keys.create(ApiKeyCreateRequest(name="kcc"))
    return issued.key


async def seed(ctx: Ctx) -> None:
    """Register the agent, its secret, the evaluators and the connection."""
    client = ctx.client
    tag = ctx.stack.run_id
    ctx.secret_value = f"kcc-secret-{secrets.token_hex(8)}"
    ctx.openai_key = f"sk-kcc-not-a-real-key-{secrets.token_hex(8)}"
    agent = await client.agents.create(AgentCreateRequest(name=f"{tag}-agent"))
    secret = await client.secrets.create(
        SecretCreateRequest(name=f"{tag}-secret", values={"KCC_SECRET_VALUE": ctx.secret_value})
    )
    version = await client.agents.create_version(
        agent.id,
        AgentVersionCreateRequest(
            display_version="kcc-1",
            run_spec=RunSpec(
                command="/app/.venv/bin/python /app/agent/probe_agent.py",
                working_dir="/app",
                env={"KCC_AGENT_ID": str(agent.id), "KCC_PLAIN": "plain-value"},
                secret_ids=[secret.id],
                timeout_seconds=900,
            ),
        ),
    )
    ctx.agent_id, ctx.agent_version_id = agent.id, version.id

    schema = {
        "title": "OpenAIConnection",
        "type": "object",
        "properties": {
            "OPENAI_API_KEY": {"title": "Openai Api Key", "type": "string", "writeOnly": True}
        },
        "required": ["OPENAI_API_KEY"],
    }
    for name, file, extra in (
        (f"{tag}-plain", "plain_evaluator.py", {}),
        (f"{tag}-judge", "judge_evaluator.py", {"provider": "openai", "connection_schema": schema}),
    ):
        blob = await client.blobs.upload(
            (HERE / "evaluators" / file).read_bytes(), media_type="text/x-python", filename=file
        )
        evaluator = await client.evaluators.create(EvaluatorCreateRequest(name=name, **extra))
        await client.evaluators.create_version(
            evaluator.id,
            EvaluatorVersionCreateRequest(
                source=ScriptPluginSource(blob_id=blob.id, entrypoint="evaluate")
            ),
        )
    await client.connections.create(
        ConnectionCreateRequest(
            name=f"{tag}-openai",
            provider="openai",
            secrets={"OPENAI_API_KEY": ctx.openai_key},
            default=True,
        )
    )
    experiment = await client.experiments.create(
        ExperimentCreateRequest(
            name=f"{tag}-experiment",
            agent_id=agent.id,
            evaluators=[
                EvaluatorConfig(evaluator=f"{tag}-plain"),
                EvaluatorConfig(evaluator=f"{tag}-judge"),
            ],
        )
    )
    ctx.experiment_id = experiment.id


async def new_run(ctx: Ctx, jobs: int = 1, sleep: float = 0) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Start one Run (an experiment run) with one job for each baseline session."""
    client = ctx.client
    now = datetime.now(UTC)
    session_ids = []
    for index in range(jobs):
        session = await client.sessions.create(
            SessionCreateRequest(
                agent_id=ctx.agent_id,
                origin=SessionOrigin.RECORDED,
                status=SessionStatus.COMPLETED,
                name=f"kcc-baseline-{secrets.token_hex(4)}",
                inputs={"sleep": sleep, "index": index},
                outputs={"baseline": True},
                started_at=now,
                ended_at=now,
                framework="kcc-probe",
                adapter_version="1",
            )
        )
        session_ids.append(session.id)
    cohort = await client.cohorts.create(
        CohortCreateRequest(name=f"kcc-cohort-{secrets.token_hex(4)}", agent_id=ctx.agent_id)
    )
    cohort_version = await client.cohorts.create_version(
        cohort.id, CohortVersionCreateRequest(add_session_ids=session_ids)
    )
    run = await client.experiments.start_run(
        ctx.experiment_id,
        ExperimentRunCreateRequest(
            cohort_version_id=cohort_version.id,
            agent_version_id=ctx.agent_version_id,
            baseline_evaluation_mode=BaselineEvaluationMode.NONE,
        ),
    )
    job_ids = [job.id async for job in client.experiment_runs.iter_jobs(run.id)]
    if len(job_ids) != jobs:
        raise RuntimeError(f"The Run has {len(job_ids)} jobs, not {jobs}")
    return run.id, job_ids


async def register(ctx: Ctx, job_id: uuid.UUID, url: str | None = None) -> dict[str, str]:
    """Register one worker with the scope of the story sc-473."""
    request = WorkerCreateRequest(
        name=f"kcc-{str(job_id)[:8]}",
        scope=WorkerScope(
            claims=[WorkerClaim(kind="agent"), WorkerClaim(kind="evaluator")],
            job_id=job_id,
        ),
        runtime=WorkerRuntime(platform="docker"),
    )
    if url is None:
        response = await ctx.client.workers.create(request)
    else:
        async with KitaruAPIClient(base_url=url, api_key=ctx.api_key) as other:
            response = await other.workers.create(request)
    return {
        "job_id": str(job_id),
        "worker_id": str(response.worker.id),
        "worker_token": response.token.get_secret_value(),
        "expires_in": round((response.token_expires_at - datetime.now(UTC)).total_seconds()),
    }


def assignment(worker: dict[str, str], timeout: int = 600, **env: str) -> dict[str, Any]:
    return {
        "job_id": worker["job_id"],
        "worker_id": worker["worker_id"],
        "worker_token": worker["worker_token"],
        "timeout": timeout,
        "env": env,
    }


async def launch(box: str, assignments: list[dict], **spec: Any) -> dict[str, Any]:
    """Run the Supervisor stand-in in a container and wait for its result."""
    payload = json.dumps({"assignments": assignments, **spec})
    code, out, err = await engine_async(
        "exec", box, "/usr/local/bin/python3", "/opt/kcc/launcher.py", payload
    )
    if code != 0:
        raise RuntimeError(f"The launcher failed ({code})\n{out}{err}")
    return json.loads(out.strip().splitlines()[-1])


def box_python(box: str, code: str) -> str:
    return engine("exec", box, "/usr/local/bin/python3", "-c", code, check=False).stdout


def box_file(box: str, path: str) -> str:
    return engine("exec", box, "cat", path, check=False).stdout


def worker_log(box: str, job_id: Any) -> list[str]:
    return box_file(box, f"/tmp/evalve/kcc/worker-{job_id}.log").splitlines()


def signal_worker(box: str, job_id: Any, name: str) -> None:
    box_python(
        box,
        "import json,os,signal;"
        f"os.kill(json.load(open('/tmp/evalve/kcc/pids.json'))['{job_id}'], signal.{name})",
    )


async def snapshot(ctx: Ctx, job_id: uuid.UUID) -> dict[str, Any]:
    job = await ctx.client.jobs.get(job_id)
    tasks = [task async for task in ctx.client.jobs.iter_tasks(job_id)]
    tasks.sort(key=lambda task: task.created)
    return {
        "job": job.status.value,
        "tasks": [
            {
                "kind": task.kind.value,
                "status": task.status.value,
                "attempt": task.attempt,
                "worker_id": str(task.worker_id) if task.worker_id else None,
                "labels": task.labels,
                "ended_at": task.ended_at,
                "error": task.error,
            }
            for task in tasks
        ],
    }


def short(state: dict[str, Any]) -> str:
    parts = [
        f"{t['kind']}:{t['status']}#{t['attempt']}@{(t['worker_id'] or 'none')[:8]}"
        for t in state["tasks"]
    ]
    return f"job={state['job']} " + " ".join(parts)


async def watch(ctx: Ctx, job_id: uuid.UUID, seconds: float, until=None) -> list[str]:
    """Record each change of a job and its tasks, with the time in seconds."""
    start = time.monotonic()
    lines: list[str] = []
    last = None
    while time.monotonic() - start < seconds:
        state = await snapshot(ctx, job_id)
        text = short(state)
        if text != last:
            lines.append(f"{time.monotonic() - start:6.1f}s {text}")
            last = text
        if until is not None and until(state):
            break
        await asyncio.sleep(1)
    return lines


async def wait_for(ctx: Ctx, job_id: uuid.UUID, predicate, seconds: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while True:
        state = await snapshot(ctx, job_id)
        if predicate(state):
            return state
        if time.monotonic() > deadline:
            raise TimeoutError(f"Timed out: {short(state)}")
        await asyncio.sleep(0.5)


def agent_running(state: dict[str, Any]) -> bool:
    return any(t["kind"] == "agent" and t["status"] == "running" for t in state["tasks"])


async def cancel(ctx: Ctx, job_id: uuid.UUID) -> None:
    try:
        await ctx.client.jobs.cancel(job_id)
    except APIError:
        pass


async def replay_outputs(ctx: Ctx, job_id: uuid.UUID) -> tuple[dict | None, dict[str, dict]]:
    """Return the report of the probe agent and the reports of the evaluators."""
    async for replay in ctx.client.replays.iter():
        if replay.job_id == job_id and replay.result_session_id:
            session = await ctx.client.sessions.get(replay.result_session_id)
            evaluations = {}
            async for evaluation in ctx.client.evaluations.iter():
                if evaluation.session_id == session.id:
                    evaluations[evaluation.name] = json.loads(evaluation.explanation or "{}")
            return session.outputs, evaluations
    return None, {}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


async def check_image(ctx: Ctx) -> None:
    """The worker image: tools, and the `kitaru` command."""
    name = "image"

    def run(image: str, *command: str) -> subprocess.CompletedProcess:
        return engine(
            "run", "--rm", "--network", "none", "--user", "1000", "--entrypoint", command[0],
            image, *command[1:], check=False,
        )  # fmt: skip

    timeout = run(WORKER_IMAGE, "/usr/bin/timeout", "--version")
    ctx.rec.expect(name, "`/usr/bin/timeout` is in the worker image", timeout.returncode == 0,
                   version=timeout.stdout.splitlines()[0] if timeout.stdout else timeout.stderr)  # fmt: skip
    find = run(WORKER_IMAGE, "find", "/app", "-maxdepth", "0", "-writable")
    ctx.rec.expect(name, "`find -writable` is in the worker image", find.returncode == 0,
                   output=find.stdout.strip() or find.stderr.strip(),
                   note="In the bare worker image, uid 1000 owns /app, so /app is printed.")  # fmt: skip
    python = run(WORKER_IMAGE, "/usr/local/bin/python3", "--version")
    ctx.rec.expect(name, "`/usr/local/bin/python3` is in the worker image", python.returncode == 0,
                   version=python.stdout.strip())  # fmt: skip
    cli = run(WORKER_IMAGE, "/app/.venv/bin/kitaru", "worker", "start", "--help")
    ctx.rec.info(name, "`/app/.venv/bin/kitaru worker start` in the bare worker image",
                 exit_code=cli.returncode, output=(cli.stdout + cli.stderr).strip()[:300])  # fmt: skip
    cli = run(AGENT_IMAGE, "/app/.venv/bin/kitaru", "worker", "start", "--help")
    ctx.rec.expect(name, "`kitaru worker start` runs after `kitaru[cli,worker]` is installed in /app/.venv",
                   cli.returncode == 0 and "--timeout" in cli.stdout)  # fmt: skip
    writable = run(AGENT_IMAGE, "find", "/app", "-writable")
    ctx.rec.expect(name, "In the test agent image, `find /app -writable` prints nothing for uid 1000",
                   writable.returncode == 0 and writable.stdout.strip() == "",
                   output=writable.stdout.strip()[:300])  # fmt: skip
    # `find` exits with code 1, because uid 1000 cannot read each directory
    # (for example /root). Only the standard output counts.
    places = run(AGENT_IMAGE, *FIND_WRITABLE).stdout.split()
    if LOCK:
        ctx.rec.expect(name, "With the block, uid 1000 can write no place of the test agent image other than `/tmp/evalve`",
                       places == [], output=places[:50])  # fmt: skip
    else:
        ctx.rec.info(name, "The places of the test agent image that uid 1000 can write (the image only, not the mounts of the engine)",
                     output=places[:50])  # fmt: skip
    mounts = run(AGENT_IMAGE, "find", "/dev", "-maxdepth", "1", "-type", "d", "-writable").stdout.split()
    ctx.rec.info(name, "The directories below `/dev` that uid 1000 can write (the engine makes them, the image cannot lock them)",
                 output=mounts)  # fmt: skip


async def check_token_mode(ctx: Ctx) -> None:
    """One job, one job-scoped worker in token mode: the main path of a Replay."""
    name = "token_mode"
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=0)
    worker = await register(ctx, job_id)
    ctx.rec.info(name, "The lifetime of a worker token from `POST /api/v1/workers`",
                 seconds=worker["expires_in"])  # fmt: skip
    box = ctx.stack.box("token-mode")
    env = box_python(box, "import os;print(sorted(k for k in os.environ if 'KEY' in k or 'CRED' in k))")
    result = await launch(box, [assignment(worker, timeout=300)])
    info = result["workers"][str(job_id)]
    state = await snapshot(ctx, job_id)
    log = worker_log(box, job_id)
    ctx.rec.expect(
        name,
        "`/app/.venv/bin/kitaru worker start --timeout N` runs with `KITARU_API_URL`, "
        "`KITARU_WORKER_ID` and `KITARU_API_TOKEN` and no key",
        state["job"] == "completed" and info["exit_code"] == 0,
        key_or_credential_variables_in_the_container=env.strip(),
        job=short(state),
        worker_log_tail=log[-6:],
    )
    ours = all(t["worker_id"] == worker["worker_id"] for t in state["tasks"])
    ctx.rec.expect(name, "The job-scoped worker claims the agent task and each evaluator task of its job",
                   ours and len(state["tasks"]) == 3 and all(t["status"] == "completed" for t in state["tasks"]),
                   tasks=short(state))  # fmt: skip
    last = max(t["ended_at"] for t in state["tasks"] if t["ended_at"])
    ctx.rec.expect(name, "A job-scoped worker exits by itself with code 0 when its job ends",
                   info["exit_code"] == 0,
                   exit_code=info["exit_code"],
                   seconds_from_last_task_end_to_exit=round(info["ended"] - last.timestamp(), 2),
                   seconds_from_start_to_exit=round(info["ended"] - info["started"], 2))  # fmt: skip

    report, evaluations = await replay_outputs(ctx, job_id)
    ctx.cache["first_report"] = report
    if report is None:
        ctx.rec.expect(name, "The probe agent recorded a session", False)
        return

    ctx.rec.expect(name, "A worker in token mode gets the Kitaru secrets of the agent version",
                   report["secret_sha256"] == sha(ctx.secret_value) and report["plain_env"] == "plain-value",
                   secret_matches=report["secret_sha256"] == sha(ctx.secret_value),
                   run_spec_env=report["plain_env"])  # fmt: skip
    ctx.rec.expect(name, "The agent process has a task token, and no key, worker ID or channel secret in its environment",
                   not report["has_kitaru_api_key"] and not report["has_loop_task_secret"]
                   and report["task_token_sha256"] != sha(worker["worker_token"]),
                   has_kitaru_api_key=report["has_kitaru_api_key"],
                   has_worker_id=report["has_worker_id"],
                   has_loop_task_secret=report["has_loop_task_secret"],
                   kitaru_and_evalve_names=[n for n in report["env_names"] if n.startswith(("KITARU", "EVALVE", "KCC", "UV_"))])  # fmt: skip

    judge = evaluations.get("kcc_judge", {})
    judge_task = next((t for t in state["tasks"] if "kitaru/provider" in t["labels"]), None)
    ctx.rec.expect(
        name,
        "A judge with the provider `openai` and a connection schema gets `OPENAI_API_KEY` "
        "from a default connection, in a job-scoped worker that holds no key",
        judge.get("key_sha256") == sha(ctx.openai_key),
        key_matches_the_connection=judge.get("key_sha256") == sha(ctx.openai_key),
        labels_of_the_judge_task=judge_task["labels"] if judge_task else None,
        plain_evaluator_has_the_key=evaluations.get("kcc_plain", {}).get("has_openai_api_key"),
    )
    ctx.rec.expect(name, "An evaluator with an inline dependency block (`openai==3.49.0`) installs and runs when root owns `/app`",
                   judge.get("openai_version") == "3.49.0" and judge.get("app_writable") is False,
                   openai_version=judge.get("openai_version"), openai_file=judge.get("openai_file"),
                   uv_cache_dir=judge.get("uv_cache_dir"), home=judge.get("home"))  # fmt: skip

    private = f"/tmp/evalve/replays/{job_id}"
    app_writes = {w["path"]: w["write"] for w in report["writes"] if w["path"].startswith(("/app", "/opt"))}
    ctx.rec.expect(
        name,
        "The agent and the evaluators start when root owns all of `/app`, `.venv` included, "
        "with a source tree that cannot be written and a new `HOME` for each worker",
        all(v != "ok" for v in app_writes.values()) and report["home"] == private
        and evaluations.get("kcc_plain", {}).get("home") == private,
        uid=report["uid"], agent_home=report["home"], agent_tmpdir=report["tmpdir"], agent_cwd=report["cwd"],
        evaluator_home=evaluations.get("kcc_plain", {}).get("home"),
        evaluator_cwd=evaluations.get("kcc_plain", {}).get("cwd"),
        writes_in_the_source_tree=app_writes,
    )

    worker_ancestors = [a for a in report["ancestors"] if a.get("worker_id")]
    token_read = any(a.get("api_token_sha256") == sha(worker["worker_token"]) for a in worker_ancestors)
    ctx.rec.expect(name, "Agent code can read the worker token from `/proc` of the worker", token_read,
                   ancestors=[{k: a.get(k) for k in ("comm", "environ_readable", "worker_id")} for a in report["ancestors"]])  # fmt: skip
    secret_read = any(a.get("loop_task_secret_sha256") == sha("kcc-channel-secret") for a in report["ancestors"])
    ctx.rec.info(name, "Agent code can read the channel secret from `/proc` of the process that started the worker",
                 read=secret_read)  # fmt: skip

    # The same path with `python -m kitaru.worker`, for comparison.
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=0)
    worker = await register(ctx, job_id)
    result = await launch(box, [assignment(worker, timeout=300)], mode="module")
    state = await snapshot(ctx, job_id)
    ctx.rec.info(name, "`python -m kitaru.worker` with `KITARU_WORKER_TIMEOUT`, in token mode",
                 exit_code=result["workers"][str(job_id)]["exit_code"], job=short(state))  # fmt: skip


async def check_files_between_runs(ctx: Ctx) -> None:
    """Two Runs in one container: which files of the first Run does the second see?"""
    name = "files_between_runs"
    box = ctx.stack.box("files")
    reports = []
    listings = []
    for _ in range(2):
        _, job_ids = await new_run(ctx, jobs=2, sleep=0)
        workers = [await register(ctx, job_id) for job_id in job_ids]
        await launch(box, [assignment(w, timeout=300) for w in workers])
        for job_id in job_ids:
            report, _ = await replay_outputs(ctx, job_id)
            reports.append(report)
        listings.append(
            box_python(
                box,
                "import os\n"
                "for root in ('/tmp','/var/tmp','/run/lock','/dev/shm','/dev/mqueue','/home/kitaru','/app','/opt'):\n"
                "    for d,_,fs in os.walk(root):\n"
                "        for f in fs:\n"
                "            p=os.path.join(d,f)\n"
                "            try:\n"
                "                if os.stat(p).st_uid==1000: print(p)\n"
                "            except OSError: pass\n",
            ).splitlines()
        )
    after = listings[-1]

    def group(path: str) -> str:
        for prefix in ("/tmp/evalve/cache/uv", "/tmp/evalve/cache/kitaru-blobs",
                       "/tmp/evalve/cache/kitaru-payloads", "/tmp/evalve/replays", "/tmp/evalve/kcc"):  # fmt: skip
            if path.startswith(prefix):
                return prefix + "/…"
        return path

    counts: dict[str, int] = {}
    for path in after:
        counts[group(path)] = counts.get(group(path), 0) + 1
    ctx.rec.info(name, "The files of uid 1000 that stay after two Runs (the private directories are deleted, as the Supervisor does)",
                 files=counts)  # fmt: skip
    ctx.rec.expect(name, "Each Replay of the two Runs completed and gave its report",
                   len([r for r in reports if r]) == 4, reports=len([r for r in reports if r]))  # fmt: skip
    second = [r for r in reports[2:] if r]
    seen = {w["path"]: w["markers_of_other_replays"] for r in second for w in r["writes"] if w["markers_of_other_replays"]}
    ctx.rec.info(name, "Marker files of an earlier Replay that a Replay of the second Run finds",
                 paths_with_markers={k: len(v) for k, v in seen.items()},
                 places_agent_code_can_write=sorted({w["path"] for r in second for w in r["writes"] if w["write"] == "ok"}))  # fmt: skip
    if LOCK:
        writes = {w["path"]: w["write"] for r in second for w in r["writes"]}
        ctx.rec.expect(name, "With the block, agent code cannot write `/tmp`, `/var/tmp`, `/run/lock` and `/home/kitaru`, and finds no marker there",
                       bool(second) and all(writes.get(p) == "EACCES" for p in BLOCKED) and not any(p in seen for p in BLOCKED),
                       writes={p: writes.get(p) for p in BLOCKED})  # fmt: skip
    private_left = [p for p in after if p.startswith("/tmp/evalve/replays")]
    ctx.rec.expect(name, "No file stays in a private directory after its worker process exits",
                   not private_left, left=private_left[:10])  # fmt: skip
    visible = sorted({d for r in reports if r for d in r["replay_dirs_visible"]})
    ctx.rec.info(name, "A Replay can list the private directories of the Replays that run at the same time (one uid)",
                 max_directories_visible=max((len(r["replay_dirs_visible"]) for r in reports if r), default=0),
                 example=visible[:3])  # fmt: skip


async def check_uv_concurrency(ctx: Ctx) -> None:
    """10 Replays at the same time on one cold uv cache."""
    name = "uv_concurrency"
    box = ctx.stack.box("uv")
    command = (
        "import subprocess,sys,time\n"
        "cmd=['uv','run','--no-project','--python','/app/.venv/bin/python','--prerelease=allow',"
        "'--with','openai==3.49.0','python','-c','import openai;print(openai.__version__)']\n"
        "env={'UV_CACHE_DIR':'/tmp/evalve/cache/uv-direct','HOME':'/tmp','PATH':'/app/.venv/bin:/usr/local/bin:/usr/bin:/bin'}\n"
        "t=time.time()\n"
        "ps=[subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(10)]\n"
        "rs=[(p.wait(),p.stdout.read().strip(),p.stderr.read().strip()[-200:]) for p in ps]\n"
        "import json;print(json.dumps({'seconds':round(time.time()-t,1),'results':rs}))\n"
    )
    direct = json.loads(box_python(box, command).strip().splitlines()[-1])
    ok = all(code == 0 and out == "3.49.0" for code, out, _ in direct["results"])
    ctx.rec.expect(name, "10 `uv run` commands at the same time are safe on one cold uv cache (the command form of Kitaru)",
                   ok, seconds=direct["seconds"], exit_codes=[r[0] for r in direct["results"]],
                   errors=[r[2] for r in direct["results"] if r[0] != 0][:2])  # fmt: skip

    _, job_ids = await new_run(ctx, jobs=10, sleep=0)
    workers = [await register(ctx, job_id) for job_id in job_ids]
    started = time.monotonic()
    result = await launch(box, [assignment(w, timeout=600) for w in workers])
    states = [await snapshot(ctx, job_id) for job_id in job_ids]
    codes = [w["exit_code"] for w in result["workers"].values()]
    ctx.rec.expect(name, "A Run with 10 jobs completes with 10 job-scoped workers in one container and one shared uv cache",
                   all(s["job"] == "completed" for s in states) and all(c == 0 for c in codes),
                   jobs=[s["job"] for s in states], exit_codes=codes,
                   seconds=round(time.monotonic() - started, 1),
                   failed_tasks=[t["error"] for s in states for t in s["tasks"] if t["status"] != "completed"][:3])  # fmt: skip
    memory = box_python(box, "print(open('/sys/fs/cgroup/memory.peak').read().strip())").strip()
    ctx.rec.info(name, "Peak memory of the container for the 10 workers (cgroup `memory.peak`, a local number, not Fargate)",
                 megabytes=round(int(memory) / 1e6) if memory.isdigit() else memory)  # fmt: skip


async def check_sigterm(ctx: Ctx) -> None:
    """SIGTERM one time and two times."""
    name = "sigterm"
    box = ctx.stack.box("sigterm")

    # One SIGTERM, while the agent task runs.
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=20)
    worker = await register(ctx, job_id)
    task = asyncio.create_task(launch(box, [assignment(worker, timeout=300)], on_sigterm="ignore"))
    await wait_for(ctx, job_id, agent_running)
    await asyncio.sleep(3)
    sent = time.time()
    signal_worker(box, job_id, "SIGTERM")
    result = await task
    info = result["workers"][str(job_id)]
    state = await snapshot(ctx, job_id)
    report, _ = await replay_outputs(ctx, job_id)
    agent = next(t for t in state["tasks"] if t["kind"] == "agent")
    ctx.rec.expect(name, "A worker that gets SIGTERM one time drains: the agent task completes and the agent gets no signal",
                   agent["status"] == "completed" and bool(report) and report["signals"] in ([], [""]),
                   exit_code=info["exit_code"], seconds_from_sigterm_to_exit=round(info["ended"] - sent, 1),
                   job=short(state), agent_signals=report["signals"] if report else None,
                   worker_log_tail=worker_log(box, job_id)[-5:])  # fmt: skip
    pending = [t for t in state["tasks"] if t["kind"] == "evaluator" and t["status"] == "pending"]
    ctx.rec.info(name, "After a drain, the evaluator tasks of the job have no worker: the job stays `running`",
                 job=state["job"], evaluator_tasks=len([t for t in state["tasks"] if t["kind"] == "evaluator"]),
                 pending=len(pending))  # fmt: skip
    await cancel(ctx, job_id)

    # Two SIGTERM.
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=120)
    worker = await register(ctx, job_id)
    task = asyncio.create_task(launch(box, [assignment(worker, timeout=300)], on_sigterm="ignore"))
    await wait_for(ctx, job_id, agent_running)
    await asyncio.sleep(3)
    signal_worker(box, job_id, "SIGTERM")
    await asyncio.sleep(2)
    sent = time.time()
    signal_worker(box, job_id, "SIGTERM")
    result = await task
    info = result["workers"][str(job_id)]
    timeline = await watch(ctx, job_id, 20, until=lambda s: s["job"] in SETTLED)
    state = await snapshot(ctx, job_id)
    agent = next(t for t in state["tasks"] if t["kind"] == "agent")
    signals = box_python(box, "import glob\nfor f in sorted(glob.glob('/tmp/evalve/kcc/agent-signals-*.log')): print(f, open(f).read().split())").strip()
    ctx.rec.expect(name, "A worker that gets SIGTERM two times cancels its tasks and does not drain",
                   info["ended"] - sent < 60 and agent["status"] != "completed",
                   exit_code=info["exit_code"], seconds_from_second_sigterm_to_exit=round(info["ended"] - sent, 1),
                   agent_task=f"{agent['status']} ({agent['error']})", job_after=timeline,
                   signals_that_the_agent_process_got=signals,
                   worker_log_tail=worker_log(box, job_id)[-6:])  # fmt: skip
    await cancel(ctx, job_id)


async def check_timeout_entrypoint(ctx: Ctx) -> None:
    """`timeout --kill-after=30 N` as the entrypoint, and its 30 s."""
    name = "timeout_entrypoint"
    limit = 30
    for on_sigterm, sleep in (("ignore", 300), ("forward", 300), ("forward", 45)):
        _, (job_id,) = await new_run(ctx, jobs=1, sleep=sleep)
        worker = await register(ctx, job_id)
        spec = json.dumps({"assignments": [assignment(worker, timeout=600)], "on_sigterm": on_sigterm})
        started = time.time()
        box = ctx.stack.box(
            f"timeout-{on_sigterm}-{sleep}",
            command=["/usr/bin/timeout", "--kill-after=30", str(limit),
                     "/usr/local/bin/python3", "/opt/kcc/launcher.py", spec],
        )  # fmt: skip
        await wait_for(ctx, job_id, agent_running)
        # Read the state of the worker 20 s after the limit, before the SIGKILL at 30 s.
        await asyncio.sleep(max(0, started + limit + 20 - time.time()))
        log = worker_log(box, job_id)
        launcher_signals = box_file(box, "/tmp/evalve/kcc/launcher-signals.log").split()
        agent_signals = box_python(box, "import glob\nfor f in glob.glob('/tmp/evalve/kcc/agent-signals-*.log'): print(open(f).read().split())").strip()
        alive = box_python(box, "import json,os\np=json.load(open('/tmp/evalve/kcc/pids.json'))\nprint([os.path.exists(f'/proc/{v}') for v in p.values()])").strip()
        at_20 = short(await snapshot(ctx, job_id))
        code = int(engine("wait", box, timeout=120).stdout.strip() or -1)
        ended = time.time()
        after = short(await snapshot(ctx, job_id))
        detail = dict(
            launcher_signals=launcher_signals,
            worker_process_alive_20s_after_the_limit=alive,
            worker_log_tail_20s_after_the_limit=log[-4:],
            agent_signals=agent_signals,
            job_20s_after_the_limit=at_20,
            container_exit_code=code,
            container_seconds=round(ended - started, 1),
            job_after_the_container_exit=after,
        )
        if on_sigterm == "ignore":
            stopped = any("Stop requested" in line or "sigterm" in line.lower() for line in log)
            ctx.rec.expect(name, "`timeout` signals only its child, not the worker processes in their own sessions",
                           launcher_signals.count("SIGTERM") >= 1 and not stopped and "True" in alive, **detail)  # fmt: skip
        elif sleep == 300:
            ctx.rec.info(name, "The 30 s of `timeout --kill-after=30`: the Supervisor sends SIGTERM one time, and the agent task needs more than 30 s",
                         **detail)  # fmt: skip
        else:
            ctx.rec.info(name, "The 30 s of `timeout --kill-after=30`: the Supervisor sends SIGTERM one time, and the agent task ends in the 30 s",
                         **detail)  # fmt: skip
        await cancel(ctx, job_id)


async def check_killed_worker(ctx: Ctx) -> None:
    """A killed worker, a second start with the same worker ID, and two workers on one ID."""
    name = "killed_worker"
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=600)
    worker = await register(ctx, job_id)
    box = ctx.stack.box("killed")
    task = asyncio.create_task(launch(box, [assignment(worker, timeout=900)]))
    await wait_for(ctx, job_id, agent_running)
    engine("kill", "--signal", "KILL", box)
    task.cancel()
    killed = time.monotonic()
    timeline = await watch(ctx, job_id, 150, until=lambda s: s["tasks"][0]["status"] == "pending")
    state = await snapshot(ctx, job_id)
    requeued = state["tasks"][0]["status"] == "pending"
    ctx.rec.expect(name, "A task that a killed worker held goes back to `pending`, and its job stays `running`",
                   requeued and state["job"] == "running",
                   seconds_from_kill_to_pending=round(time.monotonic() - killed, 1), timeline=timeline)  # fmt: skip
    still = await watch(ctx, job_id, 30)
    ctx.rec.info(name, "With no worker, the task waits as `pending` (watched for 30 s)", timeline=still)

    # A second start with the same worker ID and the same token.
    box = ctx.stack.box("second-start")
    result = await launch(box, [assignment(worker, timeout=300, KCC_SLEEP_OVERRIDE="0")])
    info = result["workers"][str(job_id)]
    state = await snapshot(ctx, job_id)
    ctx.rec.expect(name, "A second start of a worker with the same worker ID and token claims the task again and completes the job",
                   state["job"] == "completed" and info["exit_code"] == 0,
                   exit_code=info["exit_code"], job=short(state), worker_log_head=worker_log(box, job_id)[:4])  # fmt: skip

    # Two workers on one worker ID at the same time.
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=100)
    worker = await register(ctx, job_id)
    first, second = ctx.stack.box("twin-a"), ctx.stack.box("twin-b")
    task_a = asyncio.create_task(launch(first, [assignment(worker, timeout=400)]))
    await wait_for(ctx, job_id, agent_running)
    task_b = asyncio.create_task(launch(second, [assignment(worker, timeout=400)]))
    timeline = await watch(ctx, job_id, 300, until=lambda s: s["job"] in SETTLED)
    result_a, result_b = await asyncio.gather(task_a, task_b)
    state = await snapshot(ctx, job_id)
    agent = state["tasks"][0]
    ctx.rec.expect(name, "Two workers on one worker ID at the same time: the job completes and the agent task runs one time",
                   state["job"] == "completed" and agent["attempt"] == 1,
                   timeline=timeline,
                   exit_codes=[result_a["workers"][str(job_id)]["exit_code"], result_b["workers"][str(job_id)]["exit_code"]],
                   first_worker_log_tail=worker_log(first, job_id)[-5:],
                   second_worker_log_tail=worker_log(second, job_id)[-5:])  # fmt: skip


async def check_token_expiry(ctx: Ctx) -> None:
    """A worker token that expires while the worker holds a task."""
    name = "token_expiry"
    _, (job_id,) = await new_run(ctx, jobs=1, sleep=SHORT_TOKEN_SECONDS + 60)
    worker = await register(ctx, job_id, url=ctx.stack.short_url)
    box = ctx.stack.box("expiry")
    started = time.time()
    task = asyncio.create_task(launch(box, [assignment(worker, timeout=420)]))
    timeline = await watch(ctx, job_id, 330, until=lambda s: s["job"] in SETTLED)
    state = await snapshot(ctx, job_id)
    if state["job"] not in SETTLED:
        signal_worker(box, job_id, "SIGKILL")
    result = await task
    info = result["workers"][str(job_id)]
    log = worker_log(box, job_id)
    report, evaluations = await replay_outputs(ctx, job_id)
    ctx.rec.info(
        name,
        f"A worker token that expires after {worker['expires_in']} s, while the worker holds an agent task of {SHORT_TOKEN_SECONDS + 60} s",
        token_lifetime_seconds=worker["expires_in"],
        timeline=timeline,
        job_at_the_end=short(state),
        agent_session_recorded=report is not None,
        evaluations_recorded=sorted(evaluations),
        worker_exit_code=info["exit_code"],
        worker_seconds=round(info["ended"] - started, 1),
        worker_killed_by_the_check=state["job"] not in SETTLED,
        worker_log=[line for line in log if "arn" in line or "rror" in line or "401" in line][:8],
        worker_log_tail=log[-4:],
    )
    await cancel(ctx, job_id)


CHECKS = [
    check_image,
    check_token_mode,
    check_files_between_runs,
    check_uv_concurrency,
    check_sigterm,
    check_timeout_entrypoint,
    check_killed_worker,
    check_token_expiry,
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", default="", help="Run the checks whose name contains this text.")
    parser.add_argument("--keep", action="store_true", help="Do not remove the containers.")
    parser.add_argument("--lock", action="store_true",
                        help="Build the test agent image with the block of sc-534: uid 1000 can write only below /tmp/evalve.")  # fmt: skip
    args = parser.parse_args()
    global LOCK
    LOCK = args.lock
    selected = [check for check in CHECKS if args.only in check.__name__]

    print(f"Kitaru {VERSION}, engine {ENGINE}" + (", with the block (--lock)" if LOCK else ""))
    build_images()
    stack = Stack(run_id=f"kcc-{secrets.token_hex(3)}")
    rec = Recorder()
    started = datetime.now(UTC)
    failed_checks: list[str] = []
    try:
        print(f"Starting the stack {stack.run_id} ...")
        stack.up()
        api_key = await bootstrap_key(stack.url)
        async with KitaruAPIClient(base_url=stack.url, api_key=api_key) as client:
            info = await client.info.get()
            ctx = Ctx(stack=stack, client=client, api_key=api_key, rec=rec)
            await seed(ctx)
            for check in selected:
                print(f"\n== {check.__name__[6:]}: {check.__doc__.splitlines()[0]}")
                try:
                    await check(ctx)
                except Exception as error:  # noqa: BLE001
                    failed_checks.append(check.__name__)
                    rec.add(check.__name__[6:], "The check ran to its end", "FAIL", error=repr(error)[:2000])
    finally:
        if args.keep:
            print(f"\nKept the stack {stack.run_id} ({stack.url}). Remove: "
                  f"{ENGINE} rm -f $({ENGINE} ps -aq --filter name={stack.run_id})")  # fmt: skip
        else:
            stack.down()

    counts = {s: sum(1 for i in rec.items if i["status"] == s) for s in ("PASS", "FAIL", "INFO")}
    if not args.only:
        (HERE / ("results-lock.json" if LOCK else "results.json")).write_text(
            json.dumps(
                {
                    "kitaru_version": VERSION,
                    "server_version": getattr(info, "version", None),
                    "engine": ENGINE,
                    "host": f"{platform.system()} {platform.machine()}",
                    "platform": os.environ.get("DOCKER_DEFAULT_PLATFORM", "the default of the engine"),
                    "lock": LOCK,
                    "started": started.isoformat(),
                    "counts": counts,
                    "results": rec.items,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
    print(f"\n{counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['INFO']} INFO")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
