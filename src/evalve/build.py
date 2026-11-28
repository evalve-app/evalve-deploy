"""Build, check and push a committed agent image without AWS credentials.

The Dockerfile belongs to a trusted project. These checks enforce the worker
contract; they do not make arbitrary Dockerfiles reproducible or safe to run.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import tomllib
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager
from http.client import HTTPException
from importlib import resources
from pathlib import Path, PurePosixPath
from urllib.error import URLError
from urllib.request import Request, build_opener

from evalve import EvalveError, publish
from evalve.readiness import NoRedirect
from evalve.supervisor.supervisor import CONTRACT_VERSION

PLATFORM = "linux/amd64"
CHECK_SECONDS = 60
HTTP_SECONDS = 15
DIRTY = "Commit or remove your changes. evalve build builds the commit, not the working tree."
FIND_WRITABLE = [
    "/",
    "-xdev",
    "(",
    "-path",
    "/proc",
    "-o",
    "-path",
    "/sys",
    "-o",
    "-path",
    "/dev",
    "-o",
    "-path",
    "/tmp/evalve",
    ")",
    "-prune",
    "-o",
    "-writable",
    "-print",
]
# Keep the original writable-path scan unchanged, and also catch paths whose
# owner could chmod them back to writable. Runtime/private paths stay pruned.
FIND_WORKER_OWNED = [*FIND_WRITABLE[:-2], "-uid", "1000", "-print"]


class BuildInterrupted(KeyboardInterrupt):
    """A normal termination signal that must unwind through build cleanup."""

    def __init__(self, signum: int, details: str = ""):
        self.signum = signum
        super().__init__(
            f"Build interrupted by {signal.Signals(signum).name}."
            + (f" {details}" if details else "")
        )


class _BuildSignals:
    def __init__(self):
        self.signum = None
        self.cleaning = False

    def interrupt(self, signum, frame):
        # Preserve the first signal; further signals must not abort unwinding
        # or cleanup. SIGKILL remains available for forced termination.
        if self.signum is not None:
            return
        self.signum = signum
        if not self.cleaning:
            raise BuildInterrupted(signum)


@contextmanager
def termination_signals():
    state = _BuildSignals()
    if threading.current_thread() is not threading.main_thread():
        yield state
        return
    previous = {}
    try:
        for name in ("SIGTERM", "SIGHUP", "SIGINT"):
            signum = getattr(signal, name, None)
            if signum is not None:
                handler = signal.getsignal(signum)
                if handler != signal.SIG_IGN:
                    previous[signum] = handler
                    signal.signal(signum, state.interrupt)
        try:
            yield state
        except BaseException as exc:
            if state.signum is not None and not isinstance(exc, BuildInterrupted):
                raise BuildInterrupted(state.signum, str(exc)) from exc
            raise
        # Also catch a signal deferred after _build's final outcome check.
        if state.signum is not None:
            raise BuildInterrupted(state.signum)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def positive_integer(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise argparse.ArgumentTypeError(
            "--timeout-seconds must be a positive integer."
        )
    return int(value)


def add_parser(commands) -> None:
    command = commands.add_parser(
        "build", help="Build, check, push and register an agent image."
    )
    command.add_argument(
        "--agent",
        required=True,
        help="Kitaru agent name. The agent must exist, except with --no-push.",
    )
    command.add_argument(
        "--command",
        dest="agent_command",
        required=True,
        help="Command of the agent run spec.",
    )
    command.add_argument(
        "--working-dir",
        default="/app",
        help="Absolute working directory in the image (default: /app).",
    )
    command.add_argument(
        "--dockerfile",
        default="Dockerfile",
        help="Dockerfile path from the repository root.",
    )
    command.add_argument(
        "--env-file",
        type=Path,
        help="Read KITARU_API_URL and KITARU_API_KEY; the environment wins.",
    )
    command.add_argument(
        "--no-push",
        action="store_true",
        help="Build and check only. Keep the image locally, push and register nothing.",
    )
    command.add_argument(
        "--keep-image",
        action="store_true",
        help="Keep the local image after a push. --no-push always keeps it.",
    )
    command.add_argument(
        "--timeout-seconds",
        type=positive_integer,
        help="Run-spec timeout, not the build timeout.",
    )
    for name, metavar, description in (
        ("tool", "TOOL", "Capability of the agent version; repeatable."),
        (
            "secret",
            "NAME",
            "Kitaru secret to attach; repeatable. A new secret gets its value "
            "from the environment variable NAME or from a hidden prompt.",
        ),
        ("env", "NAME=value", "Plain run-spec value; repeatable."),
    ):
        command.add_argument(
            f"--{name}", action="append", default=[], metavar=metavar, help=description
        )


def validate_inputs(args) -> None:
    if not args.agent.strip():
        raise EvalveError("--agent must name a Kitaru agent.")
    tokens = shlex.split(args.agent_command)
    if not tokens:
        raise EvalveError("--command must not be empty.")
    # This is the story's narrow guard for the known uv case, not a shell or
    # uv parser. Preserve the command verbatim for the run spec.
    if tokens[:2] == ["uv", "run"] and "--no-sync" not in tokens[2:]:
        raise EvalveError(
            "The command must not sync. Use uv run --no-sync … or the Python of the environment."
        )
    if not args.working_dir.startswith("/") or "\x00" in args.working_dir:
        raise EvalveError("--working-dir must be an absolute path in the image.")
    args.working_dir = posixpath.normpath(args.working_dir)
    dockerfile = PurePosixPath(args.dockerfile)
    if (
        dockerfile.is_absolute()
        or ".." in dockerfile.parts
        or not dockerfile.parts
        or "\x00" in args.dockerfile
    ):
        raise EvalveError(
            "--dockerfile must be a path in the commit, from the repository root."
        )
    args.dockerfile = str(dockerfile)
    args.secret = list(dict.fromkeys(args.secret))
    args.tool = list(dict.fromkeys(args.tool))
    names = set()
    for name in args.secret:
        validate_name(name, "--secret")
        publish.check_secret_name(name)
    for item in args.env:
        if "=" not in item:
            raise EvalveError("--env requires NAME=value.")
        name, value = item.split("=", 1)
        validate_name(name, "--env")
        if "\x00" in value:
            raise EvalveError("--env values cannot contain a NUL character.")
        if name in names:
            raise EvalveError(f"Duplicate --env name: {name}.")
        if name in args.secret:
            raise EvalveError(f"--env name {name} is also a --secret.")
        names.add(name)


def validate_name(name: str, flag: str) -> None:
    if not name or "=" in name or "\x00" in name:
        raise EvalveError(f"{flag} requires a nonempty name without '=' or NUL.")
    if name.startswith(("EVALVE_", "KITARU_")):
        raise EvalveError(f"{flag} name {name} is reserved for Evalve or Kitaru.")


def read_settings(env_file: Path | None) -> dict[str, str]:
    settings = {}
    if env_file is not None:
        for number, line in enumerate(env_file.read_text().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if not separator:
                raise EvalveError(f"Invalid NAME=value line {number} in --env-file.")
            name = name.strip()
            if name in ("KITARU_API_URL", "KITARU_API_KEY"):
                settings[name] = value.strip()
    for name in ("KITARU_API_URL", "KITARU_API_KEY"):
        if name in os.environ:
            settings[name] = os.environ[name]
    return settings


def server_version(url: str) -> str:
    publish.check_api_url(url)
    request = Request(
        f"{url.rstrip('/')}/api/v1/info", headers={"Accept": "application/json"}
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=HTTP_SECONDS) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("reply too large")
        info = json.loads(raw)
        version = info.get("version") if isinstance(info, dict) else None
        if not isinstance(version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9_.-]*)", version
        ):
            raise ValueError("missing or invalid version")
        return version
    except (URLError, OSError, ValueError, HTTPException) as exc:
        raise EvalveError(
            "Could not read the Kitaru server version from /api/v1/info. Check KITARU_API_URL and server availability."
        ) from exc


def run_program(
    argv: list[str], *, capture_output: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    # Git's NUL-delimited filenames are arbitrary bytes. Preserve them through
    # text stdout and stdin so check-attr and archive see the same filenames.
    input_ = kwargs.pop("input", None)
    timeout = kwargs.pop("timeout", None)
    check = kwargs.pop("check", False)
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout/stderr cannot be combined with capture_output.")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input_ is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin cannot be combined with input.")
        kwargs["stdin"] = subprocess.PIPE
    with subprocess.Popen(
        argv,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        start_new_session=os.name == "posix",
        **kwargs,
    ) as process:
        try:
            stdout, stderr = process.communicate(input_, timeout=timeout)
        except BaseException:
            stop_program(process)
            raise
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def stop_program(process: subprocess.Popen) -> None:
    """Stop the program and its ordinary descendants before engine cleanup."""

    def send(signum):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signum)
            elif signum == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        send(getattr(signal, "SIGKILL", signal.SIGTERM))
        process.communicate(timeout=5)


def require_success(result, action: str) -> str:
    if result.returncode:
        # Builds stream their output. Captured check output stays private.
        raise EvalveError(
            f"{action} failed (exit {result.returncode}). Check the engine, Dockerfile and dependencies, then retry."
        )
    return result.stdout or ""


def repository(run) -> tuple[Path, str]:
    location = run(["git", "rev-parse", "--show-toplevel"])
    if location.returncode:
        raise EvalveError(
            "The directory is not a git repo. Run evalve build in a clone of the agent repo."
        )
    root = Path(location.stdout.strip())
    if require_success(
        run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root),
        "git status",
    ).strip():
        raise EvalveError(DIRTY)
    commit = require_success(
        run(["git", "rev-parse", "HEAD"], cwd=root), "Reading HEAD"
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise EvalveError("The git repo has no valid HEAD commit.")
    entries = require_success(
        run(["git", "ls-tree", "-rz", "--full-tree", commit], cwd=root),
        "Reading the committed tree",
    )
    if any(entry.startswith("160000 ") for entry in entries.split("\x00")):
        raise EvalveError(
            "Git submodules are not supported by evalve build yet. Commit ordinary source files instead."
        )
    filenames = [entry.split("\t", 1)[1] for entry in entries.split("\x00") if entry]
    attributes = require_success(
        run(
            ["git", "check-attr", "--cached", "-z", "--stdin", "filter"],
            cwd=root,
            input="".join(name + "\x00" for name in filenames),
        ),
        "Checking committed Git LFS attributes",
    ).split("\x00")
    if "lfs" in attributes[2::3]:
        raise EvalveError(
            "Git LFS files are not supported by evalve build yet. Commit ordinary source files instead."
        )
    return root, commit


def archive_context(
    run, root: Path, commit: str, directory: Path, dockerfile: str, version: str
) -> Path:
    # Read the lock directly: export-ignore must not bypass the version check.
    lock = run(["git", "show", f"{commit}:uv.lock"], cwd=root)
    if lock.returncode == 0:
        check_lock(lock.stdout, version)
    else:
        exists = run(
            ["git", "ls-tree", "--name-only", commit, "--", "uv.lock"], cwd=root
        )
        if require_success(exists, "Checking uv.lock").strip():
            raise EvalveError("Could not read uv.lock from the commit.")
    archive = directory / "source.tar"
    require_success(
        run(
            ["git", "archive", "--format=tar", "--output", str(archive), commit],
            cwd=root,
        ),
        "Archiving the commit",
    )
    context = directory / "source"
    context.mkdir()
    try:
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise EvalveError(
            "Could not unpack the committed build context. Check its paths and symbolic links."
        ) from exc
    docker_path = context / dockerfile
    if not docker_path.is_file() or not docker_path.resolve().is_relative_to(
        context.resolve()
    ):
        raise EvalveError(
            f"The Dockerfile {dockerfile} is not in the committed build context."
        )
    for path in context.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(128).startswith(
                    b"version https://git-lfs.github.com/spec/v1\n"
                ):
                    raise EvalveError(
                        "Git LFS files are not supported by evalve build yet. Commit ordinary source files instead."
                    )
    return context


def check_lock(text: str, version: str) -> None:
    try:
        packages = tomllib.loads(text).get("package", [])
        if not isinstance(packages, list) or any(
            not isinstance(p, dict) for p in packages
        ):
            raise ValueError("invalid package list")
        mismatches = [
            p
            for p in packages
            if p.get("name") == "kitaru" and p.get("version") != version
        ]
    except (ValueError, TypeError) as exc:
        raise EvalveError(
            "The committed uv.lock is invalid. Regenerate it and commit the result."
        ) from exc
    if mismatches:
        raise EvalveError(
            f"The committed uv.lock must pin kitaru at the server version {version}."
        )


class Engine:
    """Own temporary references and check containers, never shared image IDs."""

    def __init__(self, name: str, run):
        self.name = name
        self.run = run
        self.prefix = f"evalve-build-{uuid.uuid4().hex}"
        self.images: list[str] = []
        self.containers: set[str] = set()

    def call(self, *arguments: str, **kwargs):
        return self.run([self.name, *arguments], **kwargs)

    def inspect(self, image: str) -> dict:
        record = self._inspect(image)
        if record["Architecture"] != "amd64":
            # A shared tag can inspect as the host architecture even after
            # pull --platform on Docker's multi-platform image store. Never
            # compare an ARM base's layers with an AMD64 build. Engines that
            # cannot select this platform explicitly must refuse the image.
            record = self._inspect(image, platform=True)
        if record["Architecture"] != "amd64":
            raise EvalveError("The engine did not resolve a linux/amd64 image.")
        return record

    def _inspect(self, image: str, *, platform: bool = False) -> dict:
        flags = ("--platform", PLATFORM) if platform else ()
        text = require_success(
            self.call("image", "inspect", *flags, image),
            "Inspecting the linux/amd64 image (update the engine if platform selection is unsupported)",
        )
        try:
            value = json.loads(text)
            if (
                not isinstance(value, list)
                or len(value) != 1
                or not isinstance(value[0], dict)
            ):
                raise ValueError("invalid image record")
            layers = value[0]["RootFS"]["Layers"]
            if not isinstance(value[0]["Architecture"], str):
                raise ValueError("missing architecture")
            if (
                not isinstance(layers, list)
                or not layers
                or any(not isinstance(layer, str) for layer in layers)
            ):
                raise ValueError("missing layers")
            return value[0]
        except (ValueError, KeyError, TypeError) as exc:
            raise EvalveError(
                "The engine returned invalid image layers (RootFS.Layers)."
            ) from exc

    def build_image(self, context: Path, dockerfile: str, suffix: str) -> str:
        image = f"{self.prefix}:{suffix}"
        self.images.append(image)  # Also cleaned if the engine fails after tagging.
        require_success(
            self.call(
                "build",
                "--platform",
                PLATFORM,
                "--tag",
                image,
                "--file",
                str(context / dockerfile),
                str(context),
                capture_output=False,
            ),
            "Building the agent image",
        )
        return image

    def check(self, image: str, entrypoint: str, *arguments: str):
        name = f"{self.prefix}-{uuid.uuid4().hex[:12]}"
        self.containers.add(name)
        try:
            result = self.call(
                "run",
                "--rm",
                "--platform",
                PLATFORM,
                "--network",
                "none",
                "--user",
                "1000",
                "--name",
                name,
                "--env",
                "LC_ALL=C",
                "--entrypoint",
                entrypoint,
                image,
                *arguments,
                timeout=CHECK_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise EvalveError(
                f"Image check {entrypoint} timed out after {CHECK_SECONDS} seconds."
            ) from exc
        engine_errors = (
            "error response from daemon:",
            "docker: error response from daemon:",
            "cannot connect to the docker daemon",
            "error during connect:",
            "error: unable to connect to podman",
            "error: unable to start container",
        )
        if result.returncode == 125 or (
            result.returncode not in (0, 126, 127)
            and result.stderr.lower().lstrip().startswith(engine_errors)
        ):
            raise EvalveError(
                f"{self.name} could not run image check {entrypoint} (exit {result.returncode}). "
                "Check engine availability and the container configuration."
            )
        if result.returncode < 0:
            raise EvalveError(
                f"Image check {entrypoint} was terminated by signal {-result.returncode}."
            )
        # Keep the name until cleanup verifies removal. An engine error can
        # return the same exit code as a check, with --rm still incomplete.
        return result

    def cleanup(self) -> list[str]:
        failures = []
        for name in sorted(self.containers):
            try:
                result = self.call(
                    "container", "rm", "--force", "--volumes", name, timeout=15
                )
                if result.returncode:
                    listing = self.call(
                        "container",
                        "ls",
                        "--all",
                        "--format",
                        "{{.Names}}",
                        "--filter",
                        f"name={name}",
                        timeout=15,
                    )
                    if listing.returncode or name in listing.stdout.splitlines():
                        failures.append(name)
            except (OSError, subprocess.TimeoutExpired):
                failures.append(name)
        for image in reversed(self.images):
            try:
                result = self.call("image", "rm", image, timeout=15)
                if result.returncode:
                    listing = self.call(
                        "image",
                        "ls",
                        "--format",
                        "{{.Repository}}:{{.Tag}}",
                        "--filter",
                        f"reference={image}",
                        timeout=15,
                    )
                    if listing.returncode or image in listing.stdout.splitlines():
                        failures.append(image)
            except (OSError, subprocess.TimeoutExpired):
                failures.append(image)
        return failures


def check_team_image(
    engine: Engine, image: str, base_layers: list[str], version: str, working_dir: str
) -> None:
    layers = engine.inspect(image)["RootFS"]["Layers"]
    if layers[: len(base_layers)] != base_layers:
        raise EvalveError(
            f"The Dockerfile must start FROM zenmldocker/kitaru-worker:{version}, the version of the server."
        )
    worker_message = (
        "Add kitaru[cli] at the version of the server to the dependencies of the agent."
    )
    worker = engine.check(
        image,
        "/app/.venv/bin/python",
        "-c",
        "import importlib.metadata as m; print(m.version('kitaru'))",
    )
    if worker.returncode or worker.stdout.strip() != version:
        raise EvalveError(worker_message)
    if engine.check(
        image, "/app/.venv/bin/kitaru", "worker", "start", "--help"
    ).returncode:
        raise EvalveError(worker_message)
    paths = ["/app"]
    if working_dir != "/app" and not working_dir.startswith("/app/"):
        paths.append(working_dir)
    source_message = "Give /app and the working directory, their contents and their parent directories to root, without write access for uid 1000. The working directory must exist."
    tools_message = "The Dockerfile must keep executable /usr/bin/timeout and /usr/local/bin/python3."
    # Resolving symlinks also checks real parent directories, not just spelling.
    script = """import os, pathlib, sys
for value in sys.argv[1:]:
    path = pathlib.Path(value)
    if not path.is_dir():
        raise SystemExit(1)
    for parent in (path, *path.parents, *path.resolve().parents):
        if parent.lstat().st_uid != 0 or parent.stat().st_uid != 0 or os.access(parent, os.W_OK):
            raise SystemExit(1)
"""
    parents = engine.check(
        image,
        "/usr/local/bin/python3",
        "-c",
        script,
        *dict.fromkeys([*paths, working_dir]),
    )
    if parents.returncode in (126, 127):
        raise EvalveError(tools_message)
    if parents.returncode:
        raise EvalveError(source_message)
    for path in paths:
        owners = engine.check(image, "find", path, "!", "-uid", "0", "-print")
        if owners.returncode in (126, 127):
            raise EvalveError(
                "The Dockerfile must keep executable find for the image checks."
            )
        if owners.returncode or owners.stdout:
            raise EvalveError(source_message)
        result = engine.check(image, "find", path, "-writable", "-print")
        if result.returncode in (126, 127):
            raise EvalveError(
                "The Dockerfile must keep executable find for the image checks."
            )
        if result.returncode or result.stdout:
            raise EvalveError(source_message)
        result = engine.check(image, "find", path, "-name", ".env", "-print")
        if result.returncode in (126, 127):
            raise EvalveError(
                "The Dockerfile must keep executable find for the image checks."
            )
        if result.returncode or result.stdout:
            raise EvalveError(
                f"Remove .env files from {path} in the Dockerfile. Do not copy secrets into an agent image."
            )
    check_source_links(engine, image, paths, tools_message)
    if (
        engine.check(image, "/usr/bin/timeout", "--version").returncode
        or engine.check(image, "/usr/local/bin/python3", "--version").returncode
    ):
        raise EvalveError(tools_message)


def check_source_links(
    engine: Engine, image: str, paths: list[str], tools_message: str
) -> None:
    # Inspect the reachable source graph separately; the find scans retain
    # their non-following behavior. No source contents or /proc files are read.
    script = """import os, pathlib, stat, sys
visited = set()
active = set()
private = pathlib.Path('/tmp/evalve')

def fail(path, reason):
    print(str(path) + ': ' + reason)
    raise SystemExit(1)

def refuse_private(path, location):
    if path == private or private in path.parents:
        fail(location, 'source uses /tmp/evalve, which the final layer gives to the worker: ' + str(path))

def resolve_source(path, location, resolving=frozenset()):
    current = pathlib.Path('/')
    for part in path.parts[1:]:
        if not current.is_dir():
            fail(location, 'source symlink has a non-directory parent: ' + str(current))
        current = current.parent if part == '..' else current / part
        refuse_private(current, location)
        try:
            info = current.lstat()
        except OSError:
            fail(location, 'source symlink is broken or unresolved: ' + str(current))
        if info.st_uid != 0:
            fail(location, 'source target or parent is not root-owned: ' + str(current))
        if stat.S_ISLNK(info.st_mode):
            if current in resolving:
                fail(location, 'source symlink is cyclic: ' + str(current))
            try:
                target = pathlib.Path(os.readlink(current))
            except OSError:
                fail(location, 'cannot inspect source symlink: ' + str(current))
            if not target.is_absolute():
                target = current.parent / target
            current = resolve_source(target, location, resolving | {current})
        elif os.access(current, os.W_OK):
            fail(location, 'source target or parent is writable: ' + str(current))
    return current

def visit(path, origin=None):
    location = origin or path
    refuse_private(path, location)
    try:
        info = path.lstat()
    except OSError:
        fail(location, 'cannot inspect source path ' + str(path))
    if info.st_uid != 0:
        fail(location, 'source target is not root-owned: ' + str(path))
    if stat.S_ISLNK(info.st_mode):
        target = resolve_source(path, location)
        visit(target, location)
        return
    if os.access(path, os.W_OK):
        fail(location, 'source target is writable: ' + str(path))
    if not stat.S_ISDIR(info.st_mode):
        return
    identity = (info.st_dev, info.st_ino)
    if identity in active:
        fail(location, 'cyclic source directory link: ' + str(path))
    if identity in visited:
        return
    active.add(identity)
    try:
        for child in path.iterdir():
            visit(child, origin)
    except OSError:
        fail(location, 'cannot inspect source directory ' + str(path))
    active.remove(identity)
    visited.add(identity)

for value in sys.argv[1:]:
    path = pathlib.Path(value)
    resolve_source(path, path)
    visit(path)
"""
    result = engine.check(image, "/usr/local/bin/python3", "-c", script, *paths)
    if result.returncode in (126, 127):
        raise EvalveError(tools_message)
    if result.returncode:
        details = result.stdout.strip() or "The source graph could not be inspected."
        raise EvalveError(
            f"Invalid source symlink or target: {details} "
            "Keep source targets and their parents root-owned without worker write access; fix broken or cyclic links in the Dockerfile."
        )


def final_scan(engine: Engine, image: str, arguments: list[str], name: str) -> str:
    result = engine.check(image, "find", *arguments)
    if result.returncode not in (0, 1):
        raise EvalveError(
            f"The final {name} check failed (exit {result.returncode}). Check that find can run in the image."
        )
    errors = result.stderr.splitlines()
    # GNU find's unreadable-directory diagnostics are expected for uid 1000.
    # Other errors (including an engine error with exit 1) must not pass.
    permission_error = re.compile(r"find: (?:‘[^\n]*’|'[^\n]*'): Permission denied")
    if any(not permission_error.fullmatch(line) for line in errors) or (
        result.returncode == 1 and not errors
    ):
        raise EvalveError(
            f"The final {name} check could not complete. Only permission-denied find diagnostics are expected."
        )
    return result.stdout


def check_final_image(engine: Engine, image: str) -> None:
    writable = final_scan(engine, image, FIND_WRITABLE, "writable-path")
    if writable:
        raise EvalveError(
            f"The worker user can write these paths: {writable.strip()}. In an agent image, only /tmp/evalve can be written. Give the paths to root in the Dockerfile, with no write access for other users."
        )
    owned = final_scan(engine, image, FIND_WORKER_OWNED, "ownership")
    if owned:
        raise EvalveError(
            f"The image contains worker-owned paths outside /tmp/evalve: {owned.strip()}. "
            "Their owner can restore write access with chmod. Give these paths to root in the Dockerfile."
        )


def final_dockerfile(image: str, commit: str) -> str:
    return f"""FROM {image}
USER root
COPY --chown=root:root --chmod=0555 supervisor/ /opt/evalve/
RUN for place in /tmp /var/tmp /run/lock /home/kitaru; do \\
      if [ -e "$place" ]; then chown -R root:root "$place" && chmod -R go-w "$place" || exit 1; fi; \\
    done \\
    && mkdir -p /tmp/evalve && chown 1000:1000 /tmp/evalve && chmod 0700 /tmp/evalve
ENV PYTHONDONTWRITEBYTECODE=1
LABEL app.evalve.commit="{commit}" app.evalve.supervisor-contract-version="{CONTRACT_VERSION}"
USER 1000
ENTRYPOINT ["/usr/bin/timeout", "--kill-after=30", "90600", "/usr/local/bin/python3", "/opt/evalve/supervisor.py"]
CMD []
"""


def copy_supervisor(target: Path) -> None:
    def copy_tree(source, destination):
        destination.mkdir()
        for entry in source.iterdir():
            if entry.name == "__pycache__" or entry.name.endswith((".pyc", ".pyo")):
                continue
            if entry.is_dir():
                copy_tree(entry, destination / entry.name)
            elif entry.is_file():
                (destination / entry.name).write_bytes(entry.read_bytes())

    copy_tree(resources.files("evalve.supervisor"), target)


def build(args, *, run=None, get_version=None) -> str:
    with termination_signals() as interruptions:
        return _build(args, interruptions, run=run, get_version=get_version)


def _build(args, interruptions: _BuildSignals, *, run=None, get_version=None) -> str:
    validate_inputs(args)
    run = run or run_program
    get_version = get_version or server_version
    settings = read_settings(args.env_file)
    url = settings.get("KITARU_API_URL", "").strip()
    if not url:
        raise EvalveError(
            "KITARU_API_URL is missing. Set it in the environment or --env-file."
        )
    key = settings.get("KITARU_API_KEY", "").strip()
    if not args.no_push and not key:
        raise EvalveError(
            "KITARU_API_KEY is missing. Set it in the environment or --env-file, or use --no-push."
        )
    if shutil.which("git") is None:
        raise EvalveError("Install git and put it on PATH before running evalve build.")
    engine_name = next(
        (name for name in ("docker", "podman") if shutil.which(name)), None
    )
    if engine_name is None:
        raise EvalveError(
            "Install docker or podman and put it on PATH before running evalve build."
        )
    root, commit = repository(run)
    version = get_version(url)
    # The lookups and the hidden prompt come after each local refusal, and
    # before the build, so that the builder does not wait for a refusal.
    publication = None if args.no_push else publish.prepare(args, url, key)
    engine = Engine(engine_name, run)
    final_name = None
    failure = None
    temporary = tempfile.TemporaryDirectory(prefix="evalve-build-")
    try:
        directory = Path(temporary.name)
        context = archive_context(
            run, root, commit, directory, args.dockerfile, version
        )
        base = f"zenmldocker/kitaru-worker:{version}"
        print(f"Pulling {base} for {PLATFORM}…", flush=True)
        require_success(
            engine.call("pull", "--platform", PLATFORM, base, capture_output=False),
            f"{engine_name} pull (check that its daemon is running)",
        )
        base_layers = engine.inspect(base)["RootFS"]["Layers"]
        print("Building and checking the committed agent image…", flush=True)
        image = engine.build_image(context, args.dockerfile, "team")
        check_team_image(engine, image, base_layers, version, args.working_dir)
        final_context = directory / "final"
        final_context.mkdir()
        copy_supervisor(final_context / "supervisor")
        (final_context / "Dockerfile").write_text(final_dockerfile(image, commit))
        image = engine.build_image(final_context, "Dockerfile", "final")
        check_final_image(engine, image)
        invocation_id = engine.prefix.removeprefix("evalve-build-")
        tag = f"{commit[:12]}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{invocation_id}"
        if args.no_push or args.keep_image:
            candidate = f"evalve-agent-image:{tag}"
            existing = engine.call(
                "image",
                "ls",
                "--format",
                "{{.Repository}}:{{.Tag}}",
                "--filter",
                f"reference={candidate}",
            )
            if require_success(existing, "Checking the final tag").strip():
                raise EvalveError(
                    f"The local image tag {candidate} already exists. Retry the build to get a new timestamp."
                )
            require_success(
                engine.call("tag", image, candidate), "Tagging the final image"
            )
            final_name = candidate
        if publication is not None:
            publish.publish(publication, engine, args, image, tag, commit)
            # A cleanup that fails must not hide the new agent version.
            publish.report(publication)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        interruptions.cleaning = True
        failures = [
            item for item in (publish.logout(publication, engine),) if item
        ] + engine.cleanup()
        try:
            temporary.cleanup()
        except OSError:
            failures.append(temporary.name)
        retained = (
            f"Validated local image retained: {final_name}." if final_name else ""
        )
        message = (
            f"Could not clean up these temporary resources: {', '.join(failures)}."
            if failures
            else ""
        )
        # After the push, each stop gets the same text, then its reason.
        unregistered = publish.unregistered(publication)
        made = publish.made_secrets(publication) if unregistered else ""
        signum = interruptions.signum
        if isinstance(failure, BuildInterrupted):
            signum = failure.signum
        if signum is not None:
            details = [unregistered] if unregistered else []
            if failure is not None and not isinstance(failure, BuildInterrupted):
                details.append(f"Original failure: {failure}.")
            details.extend(item for item in (made, message, retained) if item)
            raise BuildInterrupted(signum, " ".join(details)) from failure
        if failures:
            message = " ".join(item for item in (message, retained) if item)
            if failure is None:
                raise EvalveError(message)
            reason = (
                " ".join(item for item in (unregistered, f"Reason: {failure}", made))
                if unregistered
                else str(failure)
            )
            raise EvalveError(f"{reason} Cleanup also failed. {message}") from failure
        if isinstance(failure, Exception) and (unregistered or retained):
            parts = (
                (unregistered, f"Reason: {failure}", made, retained)
                if unregistered
                else (str(failure), retained)
            )
            raise EvalveError(" ".join(item for item in parts if item)) from failure
    if publication is None:
        print(f"Local image: {final_name}")
        print("Nothing was pushed and nothing was registered.")
        return final_name
    if final_name:
        print(f"Local image: {final_name}")
    return final_name or publication.target
