"""The local build contract, with fake git, engine and public version request."""

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import Mock, patch

from evalve import EvalveError, build, cli

COMMIT = "abcdef0123456789" * 2 + "abcdef01"
VERSION = "0.27.6"
BASE = ["sha256:base1", "sha256:base2"]


def arguments(*extra):
    return cli.parser().parse_args(
        [
            "build",
            "--no-push",
            "--agent",
            "test",
            "--command",
            "/app/.venv/bin/python -m app",
            *extra,
        ]
    )


class Programs:
    def __init__(self):
        self.calls = []
        self.files = {"Dockerfile": b"FROM zenmldocker/kitaru-worker:0.27.6\n"}
        self.status = ""
        self.tree = "100644 blob 123\tDockerfile\x00"
        self.lock = None
        self.base_layers = BASE
        self.team_layers = BASE + ["sha256:source"]
        self.override = None
        self.contexts = []
        self.context_files = []
        self.final_dockerfile = None
        self.supervisor = None

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        result = subprocess.CompletedProcess(argv, 0, "", "")
        if self.override:
            overridden = self.override(argv, kwargs)
            if overridden is not None:
                return overridden
        if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
            result.stdout = "/fake/agent\n"
        elif argv[:3] == ["git", "rev-parse", "HEAD"]:
            result.stdout = COMMIT + "\n"
        elif argv[:2] == ["git", "status"]:
            result.stdout = self.status
        elif argv[:2] == ["git", "ls-tree"]:
            result.stdout = self.tree if "-rz" in argv else ""
        elif argv[:2] == ["git", "show"]:
            result.returncode = 128 if self.lock is None else 0
            result.stdout = self.lock or ""
        elif argv[:2] == ["git", "archive"]:
            destination = Path(argv[argv.index("--output") + 1])
            with tarfile.open(destination, "w") as archive:
                for name, content in self.files.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
        elif argv[1:3] == ["image", "inspect"]:
            layers = (
                self.team_layers if argv[-1].endswith(":team") else self.base_layers
            )
            result.stdout = json.dumps(
                [{"Architecture": "amd64", "RootFS": {"Layers": layers}}]
            )
        elif argv[1] == "build":
            context = Path(argv[-1])
            self.contexts.append(context)
            if context.name == "source":
                self.context_files = sorted(
                    str(p.relative_to(context))
                    for p in context.rglob("*")
                    if p.is_file()
                )
            else:
                self.final_dockerfile = (context / "Dockerfile").read_text()
                self.supervisor = (context / "supervisor" / "supervisor.py").read_text()
                assert not list(context.rglob("__pycache__"))
        elif argv[1] == "run":
            entrypoint = argv[argv.index("--entrypoint") + 1]
            if entrypoint == "/app/.venv/bin/python":
                result.stdout = VERSION + "\n"
            elif entrypoint == "find" and "-xdev" in argv:
                result.returncode = 1
                result.stderr = "find: '/root': Permission denied\n"
        return result


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.programs = Programs()
        self.version = Mock(return_value=VERSION)
        self.environment = patch.dict(
            os.environ,
            {
                "KITARU_API_URL": "https://kitaru.example",
                "KITARU_API_KEY": "never-send-me",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.tools = patch.object(
            build.shutil,
            "which",
            side_effect=lambda name: (
                f"/bin/{name}" if name in ("git", "docker") else None
            ),
        )
        self.tools.start()
        self.addCleanup(self.tools.stop)

    def execute(self, *extra):
        with redirect_stdout(io.StringIO()) as output:
            result = build.build(
                arguments(*extra), run=self.programs, get_version=self.version
            )
        self.output = output.getvalue()
        return result

    def assert_no_build(self):
        self.assertFalse(any(argv[1:2] == ["build"] for argv, _ in self.programs.calls))

    def test_two_builds_of_the_same_commit_in_one_second_have_distinct_names(self):
        with patch.object(build, "datetime") as clock:
            clock.now.return_value = datetime(
                2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc
            )
            first = self.execute()
            second = self.execute()
        self.assertNotEqual(first, second)
        for name in (first, second):
            self.assertRegex(
                name, r"^evalve-agent-image:[a-f0-9]{12}-[0-9]{14}-[a-f0-9]{32}$"
            )

    def test_builds_the_snapshot_checks_then_publishes_only_a_local_tag(self):
        self.programs.files["app.py"] = b"print('agent')\n"
        image = self.execute(
            "--secret",
            "OPENAI_API_KEY",
            "--env",
            "FEATURE=",
            "--timeout-seconds",
            "200",
        )
        self.assertRegex(
            image, r"^evalve-agent-image:abcdef012345-[0-9]{14}-[a-f0-9]{32}$"
        )
        self.assertIn(image, self.output)
        self.assertIn("Nothing was pushed and nothing was registered.", self.output)
        self.version.assert_called_once_with("https://kitaru.example")
        self.assertEqual(self.programs.context_files, ["Dockerfile", "app.py"])
        self.assertTrue(all(not path.exists() for path in self.programs.contexts))
        self.assertIn(f'app.evalve.commit="{COMMIT}"', self.programs.final_dockerfile)
        self.assertIn(
            f'app.evalve.supervisor-contract-version="{build.CONTRACT_VERSION}"',
            self.programs.final_dockerfile,
        )
        self.assertIn(
            "COPY --chown=root:root --chmod=0555 supervisor/ /opt/evalve/",
            self.programs.final_dockerfile,
        )
        self.assertIn("chmod -R go-w", self.programs.final_dockerfile)
        self.assertIn("--kill-after=30", self.programs.final_dockerfile)
        self.assertIn("CONTRACT_VERSION", self.programs.supervisor)
        for argv, kwargs in self.programs.calls:
            self.assertNotIn(argv[0], ("aws", "terraform"))
            self.assertFalse(
                any(
                    value in argv
                    for value in (
                        "push",
                        "login",
                        "OPENAI_API_KEY",
                        "never-send-me",
                        "FEATURE=",
                    )
                )
            )
            if argv[1:2] in (["build"], ["pull"], ["run"]):
                self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")
            if argv[1:2] in (["build"], ["pull"]):
                self.assertIs(kwargs["capture_output"], False)
            if argv[1:2] == ["run"]:
                for flag in ("--rm", "--network", "--user", "--entrypoint"):
                    self.assertIn(flag, argv)
                self.assertEqual(argv[argv.index("--network") + 1], "none")
                self.assertEqual(argv[argv.index("--user") + 1], "1000")
                self.assertEqual(kwargs["timeout"], 60)
                self.assertNotIn("capture_output", kwargs)
                self.assertNotIn("-L", argv)
                if "-xdev" in argv:
                    self.assertTrue(argv[argv.index("find") + 1].endswith(":final"))
        removed = [
            argv[-1] for argv, _ in self.programs.calls if argv[1:3] == ["image", "rm"]
        ]
        self.assertEqual(len(removed), 2)
        self.assertNotIn(image, removed)
        self.assertTrue(all(name.startswith("evalve-build-") for name in removed))

    def test_main_build_never_enters_the_aws_path(self):
        with (
            patch.object(build, "build") as execute,
            patch.object(cli, "check_tools") as tools,
            patch.object(cli, "Aws") as aws,
        ):
            self.assertEqual(
                cli.main(
                    ["build", "--agent", "test", "--command", "python", "--no-push"]
                ),
                0,
            )
        execute.assert_called_once()
        tools.assert_not_called()
        aws.assert_not_called()

    def test_help_has_no_aws_flags(self):
        with (
            redirect_stdout(io.StringIO()) as output,
            self.assertRaises(SystemExit) as exit_,
        ):
            cli.parser().parse_args(["build", "--help"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertNotIn("--profile", output.getvalue())
        self.assertNotIn("--region", output.getvalue())

    def test_requires_no_push_before_any_work(self):
        args = arguments()
        args.no_push = False
        with self.assertRaisesRegex(EvalveError, "can only run with --no-push"):
            build.build(args, run=self.programs, get_version=self.version)
        self.assertEqual(self.programs.calls, [])
        self.version.assert_not_called()

    def test_missing_tools_name_the_required_tool(self):
        for available, message in (([], "git"), (["git"], "docker or podman")):
            with (
                self.subTest(available=available),
                patch.object(
                    build.shutil,
                    "which",
                    side_effect=lambda name: name if name in available else None,
                ),
                self.assertRaisesRegex(EvalveError, message),
            ):
                self.execute()
        self.assert_no_build()

    def test_podman_only_when_docker_is_absent(self):
        with patch.object(
            build.shutil,
            "which",
            side_effect=lambda name: name if name in ("git", "podman") else None,
        ):
            self.execute()
        self.assertTrue(any(argv[0] == "podman" for argv, _ in self.programs.calls))
        self.assertFalse(any(argv[0] == "docker" for argv, _ in self.programs.calls))

    def test_broken_docker_does_not_fall_back_to_podman(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 1, "", "daemon unavailable")
            if argv[1:2] == ["pull"]
            else None
        )
        with (
            patch.object(build.shutil, "which", return_value="present"),
            self.assertRaisesRegex(EvalveError, "docker pull.*daemon"),
        ):
            self.execute()
        self.assert_no_build()
        self.assertFalse(any(argv[0] == "podman" for argv, _ in self.programs.calls))

    def test_prebuild_refusals_never_build(self):
        cases = [
            (["--command", "uv run python -m app"], "must not sync"),
            (["--working-dir", "relative"], "absolute path"),
            (["--dockerfile", "../Dockerfile"], "path in the commit"),
            (["--dockerfile", "/Dockerfile"], "path in the commit"),
            (["--command", " "], "must not be empty"),
            (["--agent", " "], "must name"),
            (["--env", "MISSING_EQUALS"], "NAME=value"),
            (["--env", "=empty"], "nonempty name"),
            (["--env", "EVALVE_A=value"], "reserved"),
            (["--env", "KITARU_A=value"], "reserved"),
            (["--secret", "EVALVE_A"], "reserved"),
            (["--secret", "KITARU_A"], "reserved"),
            (["--env", "A=a", "--secret", "A"], "also a --secret"),
            (["--env", "A=a", "--env", "A=b"], "Duplicate --env"),
        ]
        for flags, message in cases:
            with (
                self.subTest(flags=flags),
                self.assertRaisesRegex(EvalveError, message),
            ):
                self.execute(*flags)
        self.assert_no_build()
        self.version.assert_not_called()

    def test_valid_commands_and_repeatable_inputs(self):
        for command in (
            "uv run --no-sync python -m app",
            "uv run --project /app --no-sync python -m app",
            "/app/.venv/bin/python -m app",
        ):
            args = arguments(
                "--command",
                command,
                "--env",
                "FEATURE=",
                "--secret",
                "KEY",
                "--secret",
                "KEY",
                "--tool",
                "search",
                "--tool",
                "search",
            )
            build.validate_inputs(args)
            self.assertEqual(args.secret, ["KEY"])
            self.assertEqual(args.tool, ["search"])

    def test_timeout_must_be_positive_integer(self):
        for value in ("0", "-1", "1.5", "abc"):
            with (
                self.subTest(value=value),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                arguments("--timeout-seconds", value)

    def test_missing_url_is_an_early_refusal(self):
        del os.environ["KITARU_API_URL"]
        with self.assertRaisesRegex(EvalveError, "KITARU_API_URL is missing"):
            self.execute()
        self.assertEqual(self.programs.calls, [])

    def test_not_a_repo_dirty_tree_missing_dockerfile_and_submodules(self):
        with self.subTest("not a repo"):
            self.programs.override = lambda argv, _: (
                subprocess.CompletedProcess(argv, 128, "", "")
                if argv[:3] == ["git", "rev-parse", "--show-toplevel"]
                else None
            )
            with self.assertRaisesRegex(EvalveError, "not a git repo"):
                self.execute()
            self.programs.override = None
        with self.subTest("dirty"):
            self.programs.status = "?? file\n"
            with self.assertRaisesRegex(EvalveError, "Commit or remove"):
                self.execute()
            self.programs.status = ""
        with self.subTest("submodule"):
            self.programs.tree += "160000 commit 456\tsub\x00"
            with self.assertRaisesRegex(EvalveError, "submodules.*not supported"):
                self.execute()
            self.programs.tree = ""
        with self.subTest("Dockerfile"):
            self.programs.files = {}
            with self.assertRaisesRegex(
                EvalveError, "Dockerfile.*not in the committed"
            ):
                self.execute()
        self.assert_no_build()

    def test_lfs_is_refused_before_engine_work(self):
        self.programs.files["data.bin"] = (
            b"version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 3\n"
        )
        with self.assertRaisesRegex(EvalveError, "Git LFS.*not supported"):
            self.execute()
        self.assert_no_build()

    def test_lfs_attributes_are_refused_even_without_an_exported_pointer(self):
        def override(argv, _):
            if argv[:2] == ["git", "check-attr"]:
                return subprocess.CompletedProcess(
                    argv, 0, "data.bin\x00filter\x00lfs\x00", ""
                )

        self.programs.override = override
        with self.assertRaisesRegex(EvalveError, "Git LFS.*not supported"):
            self.execute()
        self.version.assert_not_called()
        self.assert_no_build()

    def test_lock_uses_the_exact_server_version_and_no_lock_is_allowed(self):
        for version in ("0.27.5", None):
            self.programs.lock = f'[[package]]\nname="kitaru"\n' + (
                f'version="{version}"\n' if version else ""
            )
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(EvalveError, "uv.lock must pin kitaru.*0.27.6"),
            ):
                self.execute()
        self.assert_no_build()
        self.programs.lock = f'[[package]]\nname="kitaru"\nversion="{VERSION}"\n'
        self.execute()
        self.programs.lock = None
        self.execute()

    def test_wrong_order_or_missing_base_layers_refuses(self):
        for layers in (BASE[::-1], [], [BASE[0]]):
            self.programs.team_layers = layers
            message = "must start FROM" if layers else "invalid image layers"
            with (
                self.subTest(layers=layers),
                self.assertRaisesRegex(EvalveError, message),
            ):
                self.execute()

    def test_inspection_selects_amd64_when_shared_tag_defaults_to_arm(self):
        def run(argv, **kwargs):
            architecture = "amd64" if "--platform" in argv else "arm64"
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    [{"Architecture": architecture, "RootFS": {"Layers": BASE}}]
                ),
                "",
            )

        engine = build.Engine("docker", Mock(side_effect=run))
        self.assertEqual(engine.inspect("base")["Architecture"], "amd64")
        self.assertEqual(
            engine.run.call_args.args[0],
            ["docker", "image", "inspect", "--platform", "linux/amd64", "base"],
        )

    def test_inspection_refuses_an_engine_that_still_returns_arm(self):
        result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps([{"Architecture": "arm64", "RootFS": {"Layers": BASE}}]),
            "",
        )
        engine = build.Engine("podman", Mock(return_value=result))
        with self.assertRaisesRegex(EvalveError, "did not resolve a linux/amd64 image"):
            engine.inspect("base")

    def test_worker_and_source_and_env_and_executables_refuse(self):
        cases = [
            (lambda a: "/app/.venv/bin/python" in a, "kitaru\\[cli\\]"),
            (lambda a: "/app/.venv/bin/kitaru" in a, "kitaru\\[cli\\]"),
            (lambda a: "-c" in a and "/usr/local/bin/python3" in a, "Give /app"),
            (
                lambda a: "find" in a and "-writable" in a and "-xdev" not in a,
                "Give /app",
            ),
            (lambda a: ".env" in a, "Remove .env"),
            (lambda a: "/usr/bin/timeout" in a, "must keep executable"),
            (
                lambda a: "/usr/local/bin/python3" in a and "--version" in a,
                "must keep executable",
            ),
        ]
        for match, message in cases:
            self.programs.override = lambda argv, _, match=match: (
                subprocess.CompletedProcess(argv, 1, "", "")
                if argv[1:2] == ["run"] and match(argv)
                else None
            )
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(EvalveError, message),
            ):
                self.execute()
            self.assertTrue(all(not path.exists() for path in self.programs.contexts))

    def test_wrong_worker_version_refuses_even_with_successful_exit(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 0, "0.27.5\n", "")
            if "/app/.venv/bin/python" in argv
            else None
        )
        with self.assertRaisesRegex(EvalveError, r"kitaru\[cli\]"):
            self.execute()

    def test_checks_a_working_directory_outside_app_and_its_parents(self):
        self.execute("--working-dir", "/opt/agent")
        finds = [
            argv
            for argv, _ in self.programs.calls
            if "find" in argv and "-xdev" not in argv
        ]
        self.assertTrue(any("/opt/agent" in argv for argv in finds))
        parents = [
            argv
            for argv, _ in self.programs.calls
            if "-c" in argv and "/usr/local/bin/python3" in argv
        ]
        self.assertIn("/opt/agent", parents[0])
        self.assertIn("path.resolve().parents", parents[0][-3])

    def test_final_find_tolerates_only_expected_permission_failures(self):
        cases = [
            (0, "", "", None),
            (1, "", "find: '/root': Permission denied\n", None),
            (
                1,
                "/bad\n",
                "find: '/root': Permission denied\n",
                "worker user can write.* /bad",
            ),
            (1, "", "", "could not complete"),
            (1, "", "find: '/data': Input/output error\n", "could not complete"),
            (1, "", "daemon unavailable\n", "could not complete"),
            (127, "", "find missing", "check failed"),
            (-9, "", "", "check failed"),
        ]
        for code, stdout, stderr, message in cases:
            result = subprocess.CompletedProcess([], code, stdout, stderr)
            engine = Mock()
            engine.check.return_value = result
            with self.subTest(code=code, stdout=stdout, stderr=stderr):
                if message:
                    with self.assertRaisesRegex(EvalveError, message):
                        build.check_final_image(engine, "final")
                else:
                    build.check_final_image(engine, "final")

    def test_final_scan_refuses_worker_owned_readonly_paths(self):
        engine = Mock()

        def check(image, entrypoint, *arguments):
            if "-uid" in arguments:
                return subprocess.CompletedProcess(
                    [], 1, "/data\n", "find: '/root': Permission denied\n"
                )
            return subprocess.CompletedProcess(
                [], 1, "", "find: '/root': Permission denied\n"
            )

        engine.check.side_effect = check
        with self.assertRaisesRegex(EvalveError, "owned.* /data"):
            build.check_final_image(engine, "final")

    def test_timeout_removes_the_check_container_and_intermediate_image(self):
        def override(argv, kwargs):
            if argv[1:2] == ["run"]:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        self.programs.override = override
        with self.assertRaisesRegex(EvalveError, "timed out after 60"):
            self.execute()
        self.assertTrue(
            any(argv[1:3] == ["container", "rm"] for argv, _ in self.programs.calls)
        )
        self.assertTrue(
            any(argv[1:3] == ["image", "rm"] for argv, _ in self.programs.calls)
        )

    def test_engine_launch_errors_are_not_reported_as_a_dockerfile_check(self):
        for code, stderr in (
            (125, "engine failure"),
            (1, "Error response from daemon: cannot launch"),
        ):
            with self.subTest(code=code):
                engine = build.Engine(
                    "docker",
                    Mock(
                        return_value=subprocess.CompletedProcess([], code, "", stderr)
                    ),
                )
                with self.assertRaisesRegex(
                    EvalveError,
                    "docker could not run image check.*Check engine availability",
                ):
                    engine.check("image", "/app/.venv/bin/python", "--version")

    def test_missing_or_nonexecutable_program_is_returned_to_its_check(self):
        for name in ("docker", "podman"):
            for code in (126, 127):
                with self.subTest(engine=name, code=code):
                    result = subprocess.CompletedProcess(
                        [],
                        code,
                        "",
                        "docker: Error response from daemon: entrypoint cannot be executed",
                    )
                    engine = build.Engine(name, Mock(return_value=result))
                    self.assertIs(engine.check("image", "/missing"), result)

    def test_interruption_cleans_resources(self):
        def override(argv, _):
            if argv[1:2] == ["run"]:
                raise KeyboardInterrupt()

        self.programs.override = override
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertTrue(
            any(argv[1:3] == ["container", "rm"] for argv, _ in self.programs.calls)
        )
        self.assertTrue(all(not path.exists() for path in self.programs.contexts))

    def test_cleanup_failure_names_retained_final_and_temporary_resources(self):
        def override(argv, _):
            if (
                argv[1:3] == ["image", "rm"]
                or argv[1:3] == ["image", "ls"]
                and "evalve-build-" in argv[-1]
            ):
                return subprocess.CompletedProcess(argv, 1, "", "daemon error")

        self.programs.override = override
        with self.assertRaisesRegex(
            EvalveError,
            "Could not clean up.*evalve-build-.*Validated local image retained: evalve-agent-image:",
        ):
            self.execute()

    def test_build_failure_cleans_temporary_context_and_references(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 1, "", "failure")
            if argv[1:2] == ["build"]
            else None
        )
        with self.assertRaisesRegex(EvalveError, "Building the agent image failed"):
            self.execute()
        self.assertTrue(
            any(argv[1:3] == ["image", "rm"] for argv, _ in self.programs.calls)
        )

    def test_cleanup_failure_preserves_the_original_build_failure(self):
        def override(argv, _):
            if (
                argv[1:2] == ["build"]
                or argv[1:3] == ["image", "rm"]
                or (argv[1:3] == ["image", "ls"] and "evalve-build-" in argv[-1])
            ):
                return subprocess.CompletedProcess(argv, 1, "", "failure")

        self.programs.override = override
        with self.assertRaisesRegex(
            EvalveError,
            "Building the agent image failed.*Cleanup also failed.*evalve-build-",
        ):
            self.execute()

    def test_interruption_keeps_its_signal_when_cleanup_also_fails(self):
        def override(argv, _):
            if argv[1:2] == ["build"]:
                raise build.BuildInterrupted(15)
            if argv[1:3] == ["image", "rm"] or (
                argv[1:3] == ["image", "ls"] and "evalve-build-" in argv[-1]
            ):
                return subprocess.CompletedProcess(argv, 1, "", "engine unavailable")

        self.programs.override = override
        with self.assertRaisesRegex(
            build.BuildInterrupted, "SIGTERM.*Could not clean up.*evalve-build-"
        ) as error:
            self.execute()
        self.assertEqual(error.exception.signum, 15)

    def test_directory_cleanup_failure_reports_the_retained_image(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cleanup = temporary.cleanup
        try:
            with (
                patch.object(
                    build.tempfile, "TemporaryDirectory", return_value=temporary
                ),
                patch.object(temporary, "cleanup", side_effect=PermissionError()),
                self.assertRaisesRegex(
                    EvalveError,
                    "Could not clean up.*Validated local image retained: evalve-agent-image:",
                ) as error,
            ):
                self.execute()
            self.assertIn(temporary.name, str(error.exception))
        finally:
            cleanup()


class SettingsTest(unittest.TestCase):
    def version_opener(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"version":"0.27.6"}'
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_remote_http_is_refused_before_any_request(self):
        for url in (
            "http://staging.example.com",
            "http://8.8.8.8",
            "http://10.0.0.1",
            "http://0.0.0.0",
            "http://localhost.example.com",
            "http://localhost.",
            "http://[2001:db8::1]",
            "http://[::]",
        ):
            with self.subTest(url=url):
                opener = self.version_opener()
                with (
                    patch.object(
                        build, "build_opener", return_value=opener
                    ) as create_opener,
                    self.assertRaisesRegex(EvalveError, "HTTPS.*localhost.*loopback"),
                ):
                    build.server_version(url)
                create_opener.assert_not_called()
                opener.open.assert_not_called()

    def test_https_and_literal_loopback_http_keep_the_public_version_probe(self):
        for url in (
            "https://staging.example.com",
            "http://localhost:8080",
            "http://127.0.0.1:8080/kitaru",
            "http://127.0.0.2",
            "http://[::1]:8080",
        ):
            with self.subTest(url=url):
                opener = self.version_opener()
                with patch.object(build, "build_opener", return_value=opener):
                    self.assertEqual(build.server_version(url), VERSION)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, url + "/api/v1/info")
                self.assertNotIn("Authorization", request.headers)

    def test_env_file_only_reads_two_names_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings"
            path.write_text(
                "# local settings\nKITARU_API_URL=https://file.example\nKITARU_API_KEY=file-key\nOTHER=ignored\n"
            )
            with patch.dict(
                os.environ, {"KITARU_API_URL": "https://env.example"}, clear=True
            ):
                self.assertEqual(
                    build.read_settings(path),
                    {
                        "KITARU_API_URL": "https://env.example",
                        "KITARU_API_KEY": "file-key",
                    },
                )
            with patch.dict(os.environ, {"KITARU_API_URL": ""}, clear=True):
                self.assertEqual(build.read_settings(path)["KITARU_API_URL"], "")

    def test_public_version_request_has_no_key_and_a_finite_timeout(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"version":"0.27.6"}'
        opener = Mock()
        opener.open.return_value = response
        with patch.object(build, "build_opener", return_value=opener):
            self.assertEqual(
                build.server_version("https://example.com/kitaru/"), VERSION
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/kitaru/api/v1/info")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)

    def test_invalid_server_reply_and_network_failure_refuse(self):
        for content in (
            b"not json",
            b"[]",
            b"{}",
            b'{"version":1}',
            b'{"version":"../../bad"}',
            b"x" * 65_537,
        ):
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = content
            opener = Mock()
            opener.open.return_value = response
            with (
                self.subTest(content=content[:40]),
                patch.object(build, "build_opener", return_value=opener),
                self.assertRaisesRegex(EvalveError, "Could not read.*server version"),
            ):
                build.server_version("https://example.com")
        with patch.object(build, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError()
            with self.assertRaisesRegex(EvalveError, "server version"):
                build.server_version("https://example.com")

    def test_credentials_in_url_are_not_sent(self):
        with (
            patch.object(build, "build_opener") as opener,
            self.assertRaisesRegex(EvalveError, "without credentials"),
        ):
            build.server_version("https://user:secret@example.com")
        opener.assert_not_called()

    def test_incomplete_http_response_is_an_actionable_error(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = IncompleteRead(b"partial-private-body", 12)
        opener = Mock()
        opener.open.return_value = response
        with (
            patch.object(build, "build_opener", return_value=opener),
            self.assertRaisesRegex(
                EvalveError, "Could not read.*server version"
            ) as error,
        ):
            build.server_version("https://example.com")
        self.assertNotIn("partial-private-body", str(error.exception))
        response.__exit__.assert_called_once()

    def test_quoted_env_file_url_names_the_cause_without_echoing_the_value(self):
        for url in ('"https://example.com"', "'https://example.com'"):
            with (
                self.subTest(url=url),
                patch.object(build, "build_opener") as opener,
                self.assertRaisesRegex(
                    EvalveError, "literal quotes.*without quotes"
                ) as error,
            ):
                build.server_version(url)
            self.assertNotIn("example.com", str(error.exception))
            opener.assert_not_called()


class ProgramRunnerTest(unittest.TestCase):
    def test_streamed_child_output_reaches_the_parent_terminal(self):
        script = """import sys
from evalve.build import run_program
result = run_program([sys.executable, '-c', "import sys; print('build progress', flush=True); print('build detail', file=sys.stderr, flush=True)"], capture_output=False)
assert result.stdout is None and result.stderr is None
raise SystemExit(result.returncode)
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("build progress", result.stdout)
        self.assertIn("build detail", result.stderr)

    def test_captured_binary_filenames_round_trip_through_git_style_stdin(self):
        output = build.run_program(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'bad-\\xff.py\\0')",
            ]
        )
        echoed = build.run_program(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            input=output.stdout,
        )
        self.assertEqual(
            echoed.stdout.encode("utf-8", "surrogateescape"), b"bad-\xff.py\x00"
        )


class GitSnapshotTest(unittest.TestCase):
    def test_real_git_excludes_ignored_env_and_uses_repo_root_from_a_subdirectory(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as target,
        ):
            root = Path(temporary)

            def git(*argv):
                return subprocess.run(
                    ["git", *argv], cwd=root, capture_output=True, text=True, check=True
                )

            git("init", "-q")
            git("config", "user.name", "Build test")
            git("config", "user.email", "build-test@example.invalid")
            (root / "Dockerfile").write_text("FROM example\n")
            (root / ".gitignore").write_text(".env\n")
            (root / "sub").mkdir()
            (root / "sub" / "app.py").write_text("print('committed')\n")
            git("add", "Dockerfile", ".gitignore", "sub/app.py")
            git("commit", "-qm", "Agent source")
            (root / ".env").write_text("SECRET=not-in-context\n")

            def run(argv, **kwargs):
                return build.run_program(argv, **({"cwd": root / "sub"} | kwargs))

            location, commit = build.repository(run)
            self.assertEqual(location.resolve(), root.resolve())
            context = build.archive_context(
                run, location, commit, Path(target), "Dockerfile", VERSION
            )
            self.assertFalse((context / ".env").exists())
            self.assertEqual(
                (context / "sub" / "app.py").read_text(), "print('committed')\n"
            )


if __name__ == "__main__":
    unittest.main()
