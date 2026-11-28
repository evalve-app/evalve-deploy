"""The push and the registration, with a fake Kitaru, a fake backend and a fake engine."""

import io
import json
import os
import signal
import subprocess
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from evalve import EvalveError, build, cli, publish
from test_build import COMMIT, VERSION, Programs

KEY = "KITKEY_builder-key"
PASSWORD = "ecr-token-password"
SECRET_VALUE = "sk-new-secret-value"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
REPOSITORY = "evalve-agent-images"
DIGEST = "sha256:" + "d" * 64
AGENT_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"
EXISTING_ID = "33333333-3333-4333-8333-333333333333"
UNREGISTERED = "is in the repository, but no agent version was registered"


class Handler(BaseHTTPRequestHandler):
    def answer(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        request = SimpleNamespace(
            method=self.command,
            path=parts.path,
            query=query,
            authorization=self.headers.get("Authorization"),
            content_type=self.headers.get("Content-Type"),
            body=json.loads(raw) if raw else None,
        )
        self.server.requests.append(request)
        status, reply, headers = self.server.reply(request)
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = answer

    def log_message(self, *arguments):
        pass


class Services(ThreadingHTTPServer):
    """Kitaru at `/api/v1` and the Evalve backend at `/evalve`, as on a Deployment."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests = []
        self.agents = {"test": AGENT_ID}
        self.secrets = {"EXISTING": EXISTING_ID}
        self.hidden = set()
        self.credentials = []
        self.secret_reply = None
        self.registration = None
        self.lookup = None
        threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def reply(self, request):
        if request.path == "/api/v1/info":
            return 200, {"version": VERSION}, {}
        if request.authorization != f"Bearer {KEY}":
            return 401, {"detail": "Not authenticated"}, {}
        key = (request.method, request.path)
        if key in (("GET", "/api/v1/agents"), ("GET", "/api/v1/secrets")):
            if self.lookup is not None:
                return self.lookup
            if set(request.query) != {"filter"}:
                return 422, {"detail": [{"msg": "Extra inputs are not permitted"}]}, {}
            wanted = json.loads(request.query["filter"][0])
            if wanted.keys() != {"field", "op", "value"} or wanted["op"] != "eq":
                return 422, {"detail": "The filter is not supported"}, {}
            known = self.agents if request.path.endswith("agents") else self.secrets
            name = wanted["value"]
            items = [{"id": known[name], "name": name}] if name in known else []
            return 200, {"items": items, "next_cursor": None}, {}
        if key == ("POST", "/api/v1/secrets"):
            if self.secret_reply is not None:
                return self.secret_reply
            name = request.body["name"]
            if name in self.secrets or name in self.hidden:
                detail = f"Secret name '{name}' is already registered"
                return 409, {"detail": detail}, {}
            self.secrets[name] = f"44444444-4444-4444-8444-{len(self.secrets):012d}"
            return 201, {"id": self.secrets[name], "name": name}, {}
        if key == ("POST", "/evalve/v1/agent-images/push-credential"):
            if self.credentials:
                return self.credentials.pop(0)
            return 200, credential(), {"Cache-Control": "no-store"}
        if key == ("POST", f"/api/v1/agents/{AGENT_ID}/versions"):
            if self.registration is not None:
                return self.registration
            return 201, {"id": VERSION_ID, "agent_id": AGENT_ID, "version": 7}, {}
        return 404, {"detail": "Not Found"}, {}

    def calls(self, method, path):
        return [r for r in self.requests if (r.method, r.path) == (method, path)]


def credential(**changes):
    return {
        "registry": REGISTRY,
        "repository": REPOSITORY,
        "username": "AWS",
        "password": PASSWORD,
        "expires_at": "2026-11-29T05:00:00Z",
        **changes,
    }


class Engine(Programs):
    """The fake engine of the local build, with the digest of a pushed name."""

    def __init__(self):
        super().__init__()
        self.digests = None

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        pushed = argv[1:3] == ["image", "inspect"] and "/" in argv[-1]
        if pushed and result.returncode == 0 and result.stdout.startswith("["):
            record = json.loads(result.stdout)[0]
            record["RepoDigests"] = (
                [f"{argv[-1].rsplit(':', 1)[0]}@{DIGEST}"]
                if self.digests is None
                else self.digests
            )
            result.stdout = json.dumps([record])
        return result

    def commands(self, name):
        return [(argv, kwargs) for argv, kwargs in self.calls if argv[1] == name]


def arguments(*extra):
    return cli.parser().parse_args(
        ["build", "--agent", "test", "--command", "/app/.venv/bin/python -m app", *extra]
    )


class PushTest(unittest.TestCase):
    def setUp(self):
        self.services = Services()
        self.addCleanup(self.services.server_close)
        self.addCleanup(self.services.shutdown)
        self.programs = Engine()
        self.version = Mock(return_value=VERSION)
        self.environment = patch.dict(
            os.environ,
            {"KITARU_API_URL": self.services.url, "KITARU_API_KEY": KEY},
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for target in (
            patch.object(
                build.shutil,
                "which",
                side_effect=lambda name: (
                    f"/bin/{name}" if name in ("git", "docker") else None
                ),
            ),
            patch.object(publish.time, "sleep"),
            patch.object(publish.sys, "stdin", Mock(isatty=Mock(return_value=False))),
            patch.object(publish.getpass, "getpass", side_effect=AssertionError),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.output = ""

    def execute(self, *extra):
        stream = io.StringIO()
        try:
            with redirect_stdout(stream), redirect_stderr(stream):
                return build.build(
                    arguments(*extra), run=self.programs, get_version=self.version
                )
        finally:
            self.output += stream.getvalue()

    def refused(self, pattern, *extra):
        with self.assertRaisesRegex(EvalveError, pattern) as caught:
            self.execute(*extra)
        return str(caught.exception)

    def pushed_tag(self):
        [(argv, _)] = self.programs.commands("push")
        prefix = f"{REGISTRY}/{REPOSITORY}:"
        self.assertTrue(argv[-1].startswith(prefix))
        return argv[-1].removeprefix(prefix)

    def registered(self):
        [request] = self.services.calls("POST", f"/api/v1/agents/{AGENT_ID}/versions")
        return request.body

    def assert_no_build(self):
        self.assertEqual(self.programs.commands("build"), [])

    def assert_logout_is_last_engine_use_of_the_registry(self):
        [(argv, _)] = self.programs.commands("logout")
        self.assertEqual(argv, ["docker", "logout", REGISTRY])
        names = [argv[1] for argv, _ in self.programs.calls]
        self.assertGreater(names.index("logout"), names.index("login"))
        if "push" in names:
            self.assertGreater(names.index("logout"), names.index("push"))

    def test_registers_one_agent_version_with_the_secrets_the_env_and_the_digest(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        self.execute(
            "--secret", "EXISTING", "--secret", "NEW_SECRET",
            "--env", "OPENAI_MODEL=gpt-4o-mini",
            "--tool", "search", "--tool", "lookup",
            "--timeout-seconds", "300", "--working-dir", "/app/src",
        )  # fmt: skip
        tag = self.pushed_tag()
        self.assertRegex(tag, r"^abcdef012345-[0-9]{14}-[a-f0-9]{32}$")
        self.assertEqual(
            self.registered(),
            {
                "display_version": tag,
                "description": f"Agent image {tag}, built by evalve build.",
                "run_spec": {
                    "command": "/app/.venv/bin/python -m app",
                    "working_dir": "/app/src",
                    "env": {
                        "OPENAI_MODEL": "gpt-4o-mini",
                        "EVALVE_AGENT_IMAGE_DIGEST": DIGEST,
                        "EVALVE_COMMIT": COMMIT,
                        "EVALVE_SUPERVISOR_CONTRACT_VERSION": str(
                            build.CONTRACT_VERSION
                        ),
                    },
                    "secret_ids": [EXISTING_ID, self.services.secrets["NEW_SECRET"]],
                    "timeout_seconds": 300,
                },
                "capabilities": {"tools": ["search", "lookup"]},
            },
        )
        [made] = self.services.calls("POST", "/api/v1/secrets")
        self.assertEqual(
            made.body, {"name": "NEW_SECRET", "values": {"NEW_SECRET": SECRET_VALUE}}
        )
        for request in self.services.requests:
            self.assertEqual(request.authorization, f"Bearer {KEY}")
            self.assertIn(request.method, ("GET", "POST"))
            self.assertNotIn("include_values", request.query)
            if request.body is not None:
                self.assertEqual(request.content_type, "application/json")
        for line in (
            "Agent: test",
            f"Agent version: {VERSION_ID} (number 7)",
            f"Image tag: {tag}",
            f"Image digest: {DIGEST}",
            "Secret EXISTING: found",
            "Secret NEW_SECRET: made",
        ):
            self.assertIn(line, self.output)
        self.assertNotIn("Nothing was pushed", self.output)
        self.assert_logout_is_last_engine_use_of_the_registry()

    def test_with_no_timeout_flag_the_run_spec_has_no_timeout(self):
        self.execute()
        body = self.registered()
        self.assertNotIn("timeout_seconds", body["run_spec"])
        self.assertEqual(body["run_spec"]["secret_ids"], [])
        self.assertEqual(body["capabilities"], {"tools": []})

    def test_the_digest_is_the_one_of_the_agent_image_repository(self):
        other = "sha256:" + "e" * 64
        self.programs.digests = [
            f"elsewhere.example/{REPOSITORY}@{other}",
            f"{REGISTRY}/{REPOSITORY}@{DIGEST}",
        ]
        self.execute()
        self.assertEqual(
            self.registered()["run_spec"]["env"]["EVALVE_AGENT_IMAGE_DIGEST"], DIGEST
        )

    def test_no_digest_or_two_digests_for_the_repository_stop_after_the_push(self):
        for digests in (
            [],
            [f"elsewhere.example/{REPOSITORY}@{DIGEST}"],
            [f"{REGISTRY}/{REPOSITORY}@sha256:short"],
            [
                f"{REGISTRY}/{REPOSITORY}@{DIGEST}",
                f"{REGISTRY}/{REPOSITORY}@sha256:{'e' * 64}",
            ],
        ):
            with self.subTest(digests=digests):
                self.programs = Engine()
                self.programs.digests = digests
                message = self.refused("no single digest")
                self.assertIn(UNREGISTERED, message)
                self.assertEqual(self.programs.commands("logout")[0][0][-1], REGISTRY)
        self.assertEqual(
            self.services.calls("POST", f"/api/v1/agents/{AGENT_ID}/versions"), []
        )

    def test_an_agent_that_kitaru_does_not_have_stops_before_the_build(self):
        with self.assertRaises(EvalveError) as caught:
            self.execute("--agent", "unknown")
        self.assertEqual(
            str(caught.exception),
            "Kitaru has no agent `unknown`. Register it with Onboarding or with `kitaru agent register`.",
        )
        self.assert_no_build()
        self.assertEqual(
            self.services.calls("POST", "/evalve/v1/agent-images/push-credential"), []
        )

    def test_a_missing_key_stops_before_each_call_and_each_program(self):
        del os.environ["KITARU_API_KEY"]
        self.refused("KITARU_API_KEY is missing")
        self.assertEqual(self.services.requests, [])
        self.assertEqual(self.programs.calls, [])
        self.version.assert_not_called()

    def test_a_key_that_kitaru_refuses_stops_before_the_build(self):
        os.environ["KITARU_API_KEY"] = "KITKEY_wrong"
        self.refused("Kitaru refused the Kitaru API key")
        self.assert_no_build()

    def test_a_key_that_a_header_cannot_hold_is_refused_and_not_shown(self):
        for key in ("KITKEY_two\nlines", "KITKEY_with space", "KITKEY_ü"):
            with self.subTest(key=key):
                os.environ["KITARU_API_KEY"] = key
                message = self.refused("KITARU_API_KEY has a character")
                self.assertNotIn(key, message)
        self.assertEqual(self.services.requests, [])

    def test_the_key_goes_only_to_https_or_to_a_loopback_address(self):
        os.environ["KITARU_API_URL"] = "http://kitaru.example"
        self.refused("must use HTTPS")
        self.assert_no_build()

    def test_a_redirect_of_kitaru_is_not_followed(self):
        self.services.lookup = (302, {}, {"Location": f"{self.services.url}/elsewhere"})
        self.refused(r"unexpected reply.*HTTP 302")
        self.assertEqual(self.services.calls("GET", "/elsewhere"), [])
        self.assert_no_build()

    def test_an_unexpected_lookup_reply_stops_before_the_build(self):
        for reply in (
            (500, {"detail": "boom"}, {}),
            (200, ["not", "a", "page"], {}),
            (200, {"items": [{"id": "../../other", "name": "test"}]}, {}),
        ):
            with self.subTest(reply=reply):
                self.services.lookup = reply
                self.refused("unexpected reply")
        self.assert_no_build()

    def test_a_secret_that_exists_is_attached_and_never_changed_or_read(self):
        os.environ["EXISTING"] = "a-newer-value"
        self.execute("--secret", "EXISTING")
        self.assertEqual(self.registered()["run_spec"]["secret_ids"], [EXISTING_ID])
        self.assertEqual(self.services.calls("POST", "/api/v1/secrets"), [])
        for request in self.services.requests:
            self.assertIn(request.method, ("GET", "POST"))
            self.assertNotIn("include_values", request.query)
            self.assertNotIn(f"/api/v1/secrets/{EXISTING_ID}", request.path)
        self.assertIn(
            "Secret `EXISTING` exists in Kitaru and was attached as it is. "
            "The value in your environment was not used.",
            self.output,
        )
        self.assertNotIn("a-newer-value", self.output)

    def test_a_secret_that_exists_with_no_value_in_the_environment_has_no_note(self):
        self.execute("--secret", "EXISTING")
        self.assertIn("Secret EXISTING: found", self.output)
        self.assertNotIn("was not used", self.output)

    def test_a_new_secret_with_no_variable_and_no_terminal_stops_before_the_build(self):
        for environment in ({}, {"NEW_SECRET": ""}):
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment
            ):
                self.refused("Kitaru has no secret NEW_SECRET", "--secret", "NEW_SECRET")
        self.assert_no_build()
        self.assertEqual(self.services.calls("POST", "/api/v1/secrets"), [])

    def test_a_new_secret_with_no_variable_is_asked_for_in_a_terminal(self):
        publish.sys.stdin.isatty.return_value = True
        with patch.object(
            publish.getpass, "getpass", return_value=SECRET_VALUE
        ) as ask:
            self.execute("--secret", "NEW_SECRET")
        ask.assert_called_once()
        self.assertIn("NEW_SECRET", ask.call_args.args[0])
        [made] = self.services.calls("POST", "/api/v1/secrets")
        self.assertEqual(made.body["values"], {"NEW_SECRET": SECRET_VALUE})

    def test_an_empty_typed_value_stops_before_the_build(self):
        publish.sys.stdin.isatty.return_value = True
        with patch.object(publish.getpass, "getpass", return_value=""):
            self.refused("Kitaru has no secret NEW_SECRET", "--secret", "NEW_SECRET")
        self.assert_no_build()

    def test_a_secret_name_that_kitaru_refuses_stops_before_each_call(self):
        for name in ("_LEADING", "TRAILING_", "two words", "dotted.name", "x" * 256):
            with self.subTest(name=name):
                self.refused("--secret name", "--secret", name)
        self.refused("--secret name", "--no-push", "--secret", "TRAILING-")
        self.assertEqual(self.services.requests, [])
        self.assertEqual(self.programs.calls, [])

    def test_no_child_process_gets_the_password_a_secret_value_or_the_key_in_argv(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        self.execute("--secret", "NEW_SECRET")
        for argv, kwargs in self.programs.calls:
            for private in (PASSWORD, SECRET_VALUE, KEY):
                self.assertFalse(any(private in argument for argument in argv))
                self.assertNotIn(private, str(kwargs.get("env", "")))
        [(argv, kwargs)] = self.programs.commands("login")
        self.assertEqual(
            argv, ["docker", "login", REGISTRY, "--username", "AWS", "--password-stdin"]
        )
        self.assertEqual(kwargs["input"], PASSWORD)
        self.assertNotIn("capture_output", kwargs)

    def test_logout_runs_after_a_failed_login_a_failed_push_and_a_failed_registration(
        self,
    ):
        def fail(command):
            return lambda argv, _: (
                subprocess.CompletedProcess(argv, 1, "", "denied")
                if argv[1] == command
                else None
            )

        for name, prepare, pattern in (
            ("login", lambda: setattr(self.programs, "override", fail("login")), "login"),
            ("tag", lambda: setattr(self.programs, "override", fail("tag")), "Tagging"),
            ("push", lambda: setattr(self.programs, "override", fail("push")), "push"),
            (
                "registration",
                lambda: setattr(
                    self.services, "registration", (500, {"detail": "boom"}, {})
                ),
                "register",
            ),
        ):
            with self.subTest(failure=name):
                self.programs = Engine()
                prepare()
                message = self.refused(pattern)
                [(argv, _)] = self.programs.commands("logout")
                self.assertEqual(argv, ["docker", "logout", REGISTRY])
                self.assertEqual(UNREGISTERED in message, name == "registration")

    def test_a_logout_that_fails_is_named_with_the_command_to_run(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 1, "", "") if argv[1] == "logout" else None
        )
        message = self.refused("Could not clean up")
        self.assertIn(f"docker logout {REGISTRY}", message)
        self.assertEqual(len(self.registered()), 4)

    def test_a_503_of_the_push_credential_route_gets_two_more_attempts(self):
        unavailable = (
            503,
            {"detail": "AWS could not give a push credential. Try again."},
            {},
        )
        self.services.credentials = [unavailable, unavailable]
        self.execute()
        path = "/evalve/v1/agent-images/push-credential"
        self.assertEqual(len(self.services.calls("POST", path)), 3)
        self.assertEqual([c.args for c in publish.time.sleep.call_args_list], [(5,)] * 2)

        self.services.requests.clear()
        self.programs = Engine()
        self.services.credentials = [unavailable] * 3
        with self.assertRaises(EvalveError) as caught:
            self.execute()
        self.assertEqual(
            str(caught.exception), "AWS could not give a push credential. Try again."
        )
        self.assertEqual(len(self.services.calls("POST", path)), 3)
        self.assertEqual(self.programs.commands("login"), [])
        self.assertEqual(self.programs.commands("logout"), [])

    def test_a_401_and_a_404_of_the_push_credential_route_stop_with_their_texts(self):
        for reply, text in (
            (
                (401, {"detail": "Not authenticated"}, {}),
                "The Evalve backend refused the Kitaru API key.",
            ),
            (
                (404, {"detail": "This deployment has no agent image repository."}, {}),
                "This deployment has no agent image repository.",
            ),
        ):
            with self.subTest(status=reply[0]):
                self.programs = Engine()
                self.services.requests.clear()
                self.services.credentials = [reply]
                with self.assertRaises(EvalveError) as caught:
                    self.execute()
                self.assertEqual(str(caught.exception), text)
                path = "/evalve/v1/agent-images/push-credential"
                self.assertEqual(len(self.services.calls("POST", path)), 1)
                self.assertEqual(self.programs.commands("login"), [])
                self.assertEqual(self.programs.commands("push"), [])

    def test_a_push_credential_that_is_not_safe_for_a_command_line_is_refused(self):
        for changes in (
            {"registry": "--config=/tmp/x"},
            {"registry": "registry.example/extra"},
            {"repository": "Upper/Case"},
            {"repository": "-flag"},
            {"username": "-u"},
            {"password": ""},
            {"password": "two\nlines"},
            {"password": 7},
        ):
            with self.subTest(changes=changes):
                self.programs = Engine()
                self.services.credentials = [(200, credential(**changes), {})]
                message = self.refused("invalid push credential")
                self.assertNotIn(str(changes.get("password", "")) or "\x00", message)
                self.assertEqual(self.programs.commands("login"), [])
        self.services.credentials = [(200, ["no", "object"], {})]
        self.refused("invalid push credential")

    def test_a_409_on_the_creation_of_a_secret_finds_the_secret_and_continues(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        real = self.services.reply

        def made_by_a_different_builder(request):
            if (request.method, request.path) == ("POST", "/api/v1/secrets"):
                self.services.secrets["NEW_SECRET"] = "55555555-5555-4555-8555-555555555555"
            return real(request)

        self.services.reply = made_by_a_different_builder
        self.execute("--secret", "NEW_SECRET")
        self.assertEqual(
            self.registered()["run_spec"]["secret_ids"],
            ["55555555-5555-4555-8555-555555555555"],
        )
        self.assertIn("Secret NEW_SECRET: found", self.output)

    def test_a_409_for_a_secret_that_the_lookup_does_not_show_stops_with_the_text(self):
        os.environ.update(FIRST=SECRET_VALUE, HIDDEN="another-private-value")
        self.services.hidden.add("HIDDEN")
        message = self.refused(
            "does not show", "--secret", "FIRST", "--secret", "HIDDEN"
        )
        tag = self.pushed_tag()
        self.assertTrue(
            message.startswith(
                f"The image `{tag}` {UNREGISTERED}. Run the command again. It makes a new image."
            )
        )
        self.assertIn("Kitaru secrets that this run made: FIRST.", message)
        self.assertEqual(
            self.services.calls("POST", f"/api/v1/agents/{AGENT_ID}/versions"), []
        )
        self.assert_logout_is_last_engine_use_of_the_registry()

    def test_a_failed_registration_after_the_push_prints_the_text_and_exits_not_0(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        self.services.registration = (
            422,
            {"detail": "Run spec references a secret that does not exist"},
            {},
        )
        stream = io.StringIO()
        with (
            patch.object(build, "run_program", self.programs),
            patch.object(build, "server_version", self.version),
            redirect_stdout(stream),
            redirect_stderr(stream),
        ):
            code = cli.main(
                ["build", "--agent", "test", "--command", "python -m app"]
                + ["--secret", "NEW_SECRET"]
            )
        self.assertEqual(code, 1)
        tag = self.pushed_tag()
        self.assertIn(
            f"Evalve: The image `{tag}` {UNREGISTERED}. Run the command again. It makes a new image.",
            stream.getvalue(),
        )
        self.assertIn("HTTP 422", stream.getvalue())
        self.assertIn("Run spec references a secret", stream.getvalue())
        self.assertIn("Kitaru secrets that this run made: NEW_SECRET.", stream.getvalue())
        self.assertNotIn("Agent version:", stream.getvalue())

    def test_a_secret_that_kitaru_refuses_to_make_stops_with_the_text(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        self.services.secret_reply = (
            422,
            {"detail": [{"msg": "Too long", "input": {"NEW_SECRET": SECRET_VALUE}}]},
            {},
        )
        message = self.refused("HTTP 422", "--secret", "NEW_SECRET")
        self.assertIn(UNREGISTERED, message)
        self.assertNotIn("Kitaru secrets that this run made", message)
        self.assertNotIn(SECRET_VALUE, message)

    def test_a_signal_after_the_push_keeps_its_code_and_prints_the_text(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        with (
            patch.object(
                publish.Api,
                "register",
                side_effect=build.BuildInterrupted(signal.SIGTERM),
            ),
            self.assertRaises(build.BuildInterrupted) as caught,
        ):
            self.execute("--secret", "NEW_SECRET")
        self.assertEqual(caught.exception.signum, signal.SIGTERM)
        self.assertIn(UNREGISTERED, str(caught.exception))
        self.assertIn(
            "Kitaru secrets that this run made: NEW_SECRET.", str(caught.exception)
        )
        self.assert_logout_is_last_engine_use_of_the_registry()

    def test_the_output_holds_no_key_no_password_and_no_secret_value(self):
        os.environ["NEW_SECRET"] = SECRET_VALUE
        echo = f"{KEY} {PASSWORD} {SECRET_VALUE}"
        self.execute("--secret", "NEW_SECRET")
        for attribute, reply, extra in (
            ("lookup", (500, {"detail": echo}, {}), ()),
            ("credentials", [(404, {"detail": echo}, {})], ()),
            ("credentials", [(503, {"detail": echo}, {})] * 3, ()),
            ("secret_reply", (422, {"detail": echo}, {}), ("--secret", "OTHER")),
            ("registration", (400, {"detail": echo}, {}), ()),
        ):
            with self.subTest(reply=attribute), patch.dict(
                os.environ, {"OTHER": SECRET_VALUE}
            ), patch.object(self.services, attribute, reply):
                self.programs = Engine()
                with self.assertRaises(EvalveError) as caught:
                    self.execute(*extra)
                self.output += str(caught.exception)
        for private in (KEY, PASSWORD, SECRET_VALUE):
            self.assertNotIn(private, self.output)

    def test_an_error_text_of_the_backend_cannot_write_control_characters_to_the_terminal(self):
        self.services.credentials = [
            (404, {"detail": "no repository\x1b[2J\r\nEvalve: all is well"}, {})
        ]
        message = self.refused("no repository")
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\n", message)

    def test_no_push_makes_no_call_with_a_credential(self):
        with (
            patch.object(build, "run_program", self.programs),
            redirect_stdout(io.StringIO()) as output,
        ):
            code = cli.main(
                ["build", "--agent", "unknown", "--command", "python -m app"]
                + ["--secret", "NEW_SECRET", "--no-push"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Nothing was pushed and nothing was registered.", output.getvalue())
        self.assertEqual(
            [(r.method, r.path, r.authorization) for r in self.services.requests],
            [("GET", "/api/v1/info", None)],
        )
        for command in ("login", "logout", "push"):
            self.assertEqual(self.programs.commands(command), [])

    def test_a_push_keeps_no_local_image(self):
        self.execute()
        tag = self.pushed_tag()
        tagged = [argv[-1] for argv, _ in self.programs.commands("tag")]
        self.assertEqual(tagged, [f"{REGISTRY}/{REPOSITORY}:{tag}"])
        removed = [
            argv[-1] for argv, _ in self.programs.calls if argv[1:3] == ["image", "rm"]
        ]
        self.assertEqual(len(removed), 3)
        self.assertIn(f"{REGISTRY}/{REPOSITORY}:{tag}", removed)
        self.assertEqual(
            sorted(name.rsplit(":", 1)[1] for name in removed if "/" not in name),
            ["final", "team"],
        )
        self.assertNotIn("Local image", self.output)

    def test_a_failed_push_keeps_no_local_image(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 1, "", "") if argv[1] == "push" else None
        )
        message = self.refused("push failed")
        self.assertNotIn("retained", message)
        removed = [
            argv[-1] for argv, _ in self.programs.calls if argv[1:3] == ["image", "rm"]
        ]
        self.assertEqual(len(removed), 3)

    def test_keep_image_keeps_the_local_name_after_a_push(self):
        self.execute("--keep-image")
        tag = self.pushed_tag()
        tagged = [argv[-1] for argv, _ in self.programs.commands("tag")]
        self.assertEqual(
            tagged, [f"evalve-agent-image:{tag}", f"{REGISTRY}/{REPOSITORY}:{tag}"]
        )
        removed = [
            argv[-1] for argv, _ in self.programs.calls if argv[1:3] == ["image", "rm"]
        ]
        self.assertNotIn(f"evalve-agent-image:{tag}", removed)
        self.assertIn(f"{REGISTRY}/{REPOSITORY}:{tag}", removed)
        self.assertIn(f"Local image: evalve-agent-image:{tag}", self.output)

    def test_keep_image_keeps_the_local_name_after_a_failed_push(self):
        self.programs.override = lambda argv, _: (
            subprocess.CompletedProcess(argv, 1, "", "") if argv[1] == "push" else None
        )
        message = self.refused("push failed", "--keep-image")
        [local] = [
            argv[-1]
            for argv, _ in self.programs.commands("tag")
            if argv[-1].startswith("evalve-agent-image:")
        ]
        self.assertIn(f"Validated local image retained: {local}.", message)
        removed = [
            argv[-1] for argv, _ in self.programs.calls if argv[1:3] == ["image", "rm"]
        ]
        self.assertNotIn(local, removed)

    def test_keep_image_with_no_push_gives_no_error_and_changes_nothing(self):
        image = self.execute("--no-push", "--keep-image")
        self.assertRegex(image, r"^evalve-agent-image:")
        self.assertIn("Nothing was pushed and nothing was registered.", self.output)
        self.assertEqual(self.services.requests, [])


if __name__ == "__main__":
    unittest.main()
