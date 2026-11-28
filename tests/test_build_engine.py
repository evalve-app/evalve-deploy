"""Small real-engine fixture for the image checks; no Kitaru server needed."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from evalve import EvalveError, build

ENGINE = shutil.which("docker") or shutil.which("podman")


@unittest.skipUnless(ENGINE, "docker or podman is not on PATH")
class EngineTest(unittest.TestCase):
    def test_real_image_checks_and_the_final_write_boundary(self):
        # Stand-ins for the worker package/CLI keep this fixture small. The
        # real engine still exercises ownership, GNU find, timeout, resource
        # copying, the generated final layer, and every container invocation.
        engine = build.Engine(Path(ENGINE).name, build.run_program)
        started = time.monotonic()
        try:
            base = "python:3.11-slim"
            build.require_success(
                engine.call("pull", "--platform", build.PLATFORM, base),
                "Pulling the test fixture base",
            )
            layers = engine.inspect(base)["RootFS"]["Layers"]
            with tempfile.TemporaryDirectory(prefix="evalve-engine-test-") as temporary:
                directory = Path(temporary)
                (directory / "Dockerfile").write_text(f"""FROM {base}
RUN groupadd --gid 1000 kitaru && useradd --uid 1000 --gid 1000 kitaru \\
    && mkdir -p /app/.venv/bin /var/tmp /run/lock /home/kitaru \\
    && ln -s /usr/local/bin/python3 /app/.venv/bin/python \\
    && mkdir -p /usr/local/lib/python3.11/site-packages/kitaru-0.0.0.dist-info \\
    && printf 'Name: kitaru\\nVersion: 0.0.0\\n' > /usr/local/lib/python3.11/site-packages/kitaru-0.0.0.dist-info/METADATA \\
    && printf '#!/bin/sh\\n[ "$*" = "worker start --help" ]\\n' > /app/.venv/bin/kitaru \\
    && chmod 0555 /app/.venv/bin/kitaru \\
    && chown -R root:root /app && chmod -R go-w /app \\
    && touch /tmp/world-writable.lock && chmod 0666 /tmp/world-writable.lock
""")
                team = engine.build_image(directory, "Dockerfile", "team")
                build.check_team_image(engine, team, layers, "0.0.0", "/app")
                (directory / "Dockerfile.safe-links").write_text(
                    f"FROM {team}\nRUN mkdir /opt/agent && touch /opt/agent/module.py "
                    "&& ln -s /opt/agent/module.py /app/module.py "
                    "&& ln -s /opt/agent /app/agent\n"
                )
                safe_links = engine.build_image(
                    directory, "Dockerfile.safe-links", "safe-links"
                )
                build.check_team_image(engine, safe_links, layers, "0.0.0", "/app")
                source_links = (
                    (
                        "owned-target",
                        "RUN mkdir /opt/agent && touch /opt/agent/module.py && chown 1000:1000 /opt/agent/module.py && chmod 0444 /opt/agent/module.py && ln -s /opt/agent/module.py /app/module.py\n",
                    ),
                    (
                        "owned-target-parent",
                        "RUN mkdir /opt/agent && touch /opt/agent/module.py && chmod 0444 /opt/agent/module.py && chown 1000:1000 /opt/agent && chmod 0555 /opt/agent && ln -s /opt/agent/module.py /app/module.py\n",
                    ),
                    (
                        "private-target",
                        "RUN mkdir /tmp/evalve && touch /tmp/evalve/module.py && chmod 0444 /tmp/evalve/module.py && ln -s /tmp/evalve/module.py /app/module.py\n",
                    ),
                    (
                        "locked-private-target",
                        "RUN chmod 0755 /tmp && mkdir /tmp/evalve && touch /tmp/evalve/module.py && ln -s /tmp/evalve/module.py /app/module.py\n",
                    ),
                    (
                        "locked-private-parent-target",
                        "RUN chmod -R go-w /tmp && mkdir /tmp/evalve && ln -s /tmp /app/agent\n",
                    ),
                    (
                        "nested-target",
                        "RUN mkdir /opt/agent /tmp/evalve && touch /tmp/evalve/module.py && chmod 0444 /tmp/evalve/module.py && ln -s /tmp/evalve/module.py /opt/agent/module.py && ln -s /opt/agent /app/agent\n",
                    ),
                    (
                        "mutable-link-chain",
                        "RUN mkdir /opt/agent /tmp/evalve && touch /opt/agent/module.py && ln -s /opt/agent/module.py /tmp/evalve/alias.py && ln -s /tmp/evalve/alias.py /app/module.py\n",
                    ),
                    ("broken-target", "RUN ln -s /opt/missing.py /app/module.py\n"),
                    ("cyclic-target", "RUN ln -s module.py /app/module.py\n"),
                    ("directory-cycle", "RUN ln -s /app /app/agent\n"),
                )
                for name, instruction in source_links:
                    with self.subTest(source_link=name):
                        path = f"Dockerfile.{name}"
                        (directory / path).write_text(f"FROM {team}\n{instruction}")
                        linked = engine.build_image(directory, path, name)
                        with self.assertRaisesRegex(
                            EvalveError, "/app/(module.py|agent)"
                        ):
                            build.check_team_image(
                                engine, linked, layers, "0.0.0", "/app"
                            )
                nonroot_source = (
                    (
                        "locked-private-working-dir",
                        "RUN chmod 0755 /tmp && mkdir -p /tmp/evalve/src\n",
                        "/tmp/evalve/src",
                        "/tmp/evalve",
                    ),
                    (
                        "owned-file",
                        "RUN touch /app/owned.py && chown 1000:1000 /app/owned.py && chmod 0444 /app/owned.py\n",
                        "/app",
                        "root",
                    ),
                    (
                        "owned-directory",
                        "RUN mkdir /app/owned && chown 1000:1000 /app/owned && chmod 0555 /app/owned\n",
                        "/app",
                        "root",
                    ),
                    (
                        "owned-parent",
                        "RUN mkdir -p /opt/source/agent && chown 1000:1000 /opt/source && chmod 0555 /opt/source\n",
                        "/opt/source/agent",
                        "root",
                    ),
                )
                for name, instruction, working_dir, message in nonroot_source:
                    with self.subTest(nonroot_source=name):
                        path = f"Dockerfile.{name}"
                        (directory / path).write_text(f"FROM {team}\n{instruction}")
                        nonroot = engine.build_image(directory, path, name)
                        with self.assertRaisesRegex(EvalveError, message):
                            build.check_team_image(
                                engine, nonroot, layers, "0.0.0", working_dir
                            )
                # Exercise actual engine exit 126/127 with daemon-prefixed
                # stderr. These must reach the check's Dockerfile guidance.
                missing_programs = (
                    (
                        "no-venv",
                        f"FROM {base}\nRUN mkdir -p /app\n",
                        r"Add kitaru\[cli\]",
                    ),
                    (
                        "no-cli",
                        f"FROM {team}\nRUN rm /app/.venv/bin/kitaru\n",
                        r"Add kitaru\[cli\]",
                    ),
                    (
                        "nonexecutable-cli",
                        f"FROM {team}\nRUN chmod 0644 /app/.venv/bin/kitaru\n",
                        r"Add kitaru\[cli\]",
                    ),
                    (
                        "no-timeout",
                        f"FROM {team}\nRUN rm /usr/bin/timeout\n",
                        "must keep executable /usr/bin/timeout and /usr/local/bin/python3",
                    ),
                    (
                        "no-system-python",
                        f"FROM {team}\nRUN ln -sf /usr/local/bin/python3.11 /app/.venv/bin/python && rm /usr/local/bin/python3\n",
                        "must keep executable /usr/bin/timeout and /usr/local/bin/python3",
                    ),
                    (
                        "no-find",
                        f"FROM {team}\nRUN rm /usr/bin/find\n",
                        "must keep executable find",
                    ),
                )
                for name, dockerfile, message in missing_programs:
                    with self.subTest(missing=name):
                        path = f"Dockerfile.{name}"
                        (directory / path).write_text(dockerfile)
                        missing = engine.build_image(directory, path, name)
                        with self.assertRaisesRegex(EvalveError, message):
                            build.check_team_image(
                                engine, missing, layers, "0.0.0", "/app"
                            )
                (directory / "app.env").write_text("do not copy this\n")
                # Exercise a genuine refusal, not just the successful fixture.
                (directory / "Dockerfile.bad").write_text(
                    f"FROM {team}\nRUN touch /app/.env\n"
                )
                bad = engine.build_image(directory, "Dockerfile.bad", "bad")
                with self.assertRaisesRegex(EvalveError, "Remove .env"):
                    build.check_team_image(engine, bad, layers, "0.0.0", "/app")
                final_context = directory / "final"
                final_context.mkdir()
                build.copy_supervisor(final_context / "supervisor")
                (final_context / "Dockerfile").write_text(
                    build.final_dockerfile(team, "a" * 40)
                )
                final = engine.build_image(final_context, "Dockerfile", "final")
                build.check_final_image(engine, final)
                for name, instruction in (
                    (
                        "owned-data-directory",
                        "RUN mkdir /data && chown 1000:1000 /data && chmod 0555 /data\n",
                    ),
                    (
                        "owned-data-file",
                        "RUN touch /opt/data && chown 1000:1000 /opt/data && chmod 0444 /opt/data\n",
                    ),
                ):
                    with self.subTest(final_owner=name):
                        path = f"Dockerfile.{name}"
                        (directory / path).write_text(
                            f"FROM {final}\nUSER root\n{instruction}USER 1000\n"
                        )
                        owned = engine.build_image(directory, path, name)
                        with self.assertRaisesRegex(
                            EvalveError, "owned.*(/data|/opt/data)"
                        ):
                            build.check_final_image(engine, owned)
                code = """import os
from pathlib import Path
Path('/tmp/evalve/success').write_text('yes')
for directory in ('/tmp', '/var/tmp', '/run/lock', '/home/kitaru'):
    try:
        Path(directory, 'must-not-write').write_text('no')
    except PermissionError:
        pass
    else:
        raise SystemExit('could write ' + directory)
assert not os.access('/tmp/world-writable.lock', os.W_OK)
assert Path('/opt/evalve/supervisor.py').is_file()
"""
                build.require_success(
                    engine.check(final, "/usr/local/bin/python3", "-c", code),
                    "Checking final image write permissions",
                )
        finally:
            failures = engine.cleanup()
            self.assertEqual(failures, [], f"Remove these test resources: {failures}")
        print(
            f"Real-engine image checks ({build.PLATFORM}): {time.monotonic() - started:.2f}s",
            flush=True,
        )


if __name__ == "__main__":
    unittest.main()
