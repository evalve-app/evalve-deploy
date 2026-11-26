import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra import refresh


class RefreshTest(unittest.TestCase):
    config = {
        "application_secret_arn": "app-secret",
        "database_secret_arn": "db-secret",
        "backend_secret_arn": "backend-secret",
        "database_host": "db.example.com",
        "hostname": "evalve.example.com",
        "region": "us-east-1",
    }
    application = {
        "jwt_signing_key": "signing-key",
        "encryption_key": "encryption-key",
        "admin_password": "bootstrap-password",
    }

    def test_rotated_password_replaces_private_file_and_restarts_server(self):
        database = {"username": "evalve_admin", "password": "old"}
        secrets = {"app-secret": self.application, "db-secret": database}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                refresh, "get_secret", side_effect=lambda arn, region: secrets[arn]
            ),
            patch.object(refresh.subprocess, "run") as run,
        ):
            destination = Path(directory) / "server.env"
            refresh.refresh(self.config, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertIn("KITARU_SERVER_DB_PWD=old\n", destination.read_text())

            refresh.refresh(self.config, destination, restart=True)
            run.assert_not_called()

            database["password"] = "rotated"
            refresh.refresh(self.config, destination, restart=True)
            run.assert_called_once_with(
                ["systemctl", "try-restart", "evalve-kitaru"], check=True
            )
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            env = destination.read_text()
            self.assertIn("KITARU_SERVER_DB_PWD=rotated\n", env)
            self.assertIn("KITARU_SERVER_DB_SSL_MODE=verify-full\n", env)
            self.assertIn("KITARU_SERVER_AUTH_SCHEME=local\n", env)
            self.assertIn("KITARU_SERVER_AUTH_COOKIE_SECURE=true\n", env)
            self.assertIn("KITARU_SERVER_SERVER_URL=https://evalve.example.com\n", env)

    def test_secret_cannot_add_environment_variables(self):
        database = {"username": "evalve_admin", "password": "unsafe\nINJECTED=value"}
        with self.assertRaises(ValueError):
            refresh.build_environment(self.config, self.application, database)

    def test_restore_fetches_pinned_application_version_and_current_database_secret(
        self,
    ):
        config = dict(self.config, application_secret_version="saved-version")
        database = {"username": "evalve_admin", "password": "restored-db-password"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                refresh, "get_secret", side_effect=[self.application, database]
            ) as read,
        ):
            refresh.refresh(config, Path(directory) / "server.env")
        self.assertEqual(
            [call.args for call in read.call_args_list],
            [
                ("app-secret", "us-east-1", "--version-id", "saved-version"),
                ("db-secret", "us-east-1"),
            ],
        )

    database = {"username": "evalve_admin", "password": "master-password"}
    backend = {"db_user": "evalve", "db_password": "role-password"}
    image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64

    def secrets(self, database=None, backend=None):
        values = {
            "app-secret": self.application,
            "db-secret": database or self.database,
            "backend-secret": backend or self.backend,
        }
        return lambda arn, region: values[arn]

    def docker(self, calls, returncode=0, timeout=False):
        """A fake subprocess.run that records Docker and systemctl calls."""

        def run(command, **options):
            calls.append((command, options))
            if command[:2] == ["docker", "run"]:
                if timeout:
                    raise subprocess.TimeoutExpired(command, options["timeout"])
                return subprocess.CompletedProcess(command, returncode)
            return subprocess.CompletedProcess(command, 0)

        return run

    def test_backend_gets_only_its_role_login_in_a_private_file(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret", side_effect=self.secrets()) as read,
        ):
            destination = Path(directory) / "backend.env"
            refresh.refresh_backend(self.config, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(
                destination.read_text(),
                "EVALVE_DB_HOST=db.example.com\n"
                "EVALVE_DB_PORT=5432\n"
                "EVALVE_DB_USER=evalve\n"
                "EVALVE_DB_PWD=role-password\n",
            )
        read.assert_called_once_with("backend-secret", "us-east-1")

    def test_a_new_backend_password_restarts_the_backend(self):
        backend = dict(self.backend)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret", side_effect=self.secrets(backend=backend)),
            patch.object(refresh.subprocess, "run") as run,
        ):
            destination = Path(directory) / "backend.env"
            refresh.refresh_backend(self.config, destination)
            refresh.refresh_backend(self.config, destination, restart=True)
            run.assert_not_called()

            backend["db_password"] = "rotated"
            refresh.refresh_backend(self.config, destination, restart=True)
            run.assert_called_once_with(
                ["systemctl", "try-restart", "evalve-backend"], check=True
            )
            self.assertIn("EVALVE_DB_PWD=rotated\n", destination.read_text())

    def test_backend_secret_cannot_add_environment_variables(self):
        backend = {"db_user": "evalve", "db_password": "unsafe\nINJECTED=value"}
        with self.assertRaises(ValueError):
            refresh.build_backend_environment(self.config, backend)
        with self.assertRaises(ValueError):
            refresh.build_setup_environment(self.config, self.database, backend)

    def run_timer(self, root, backend_running, database=None):
        config_file = root / "config.json"
        config_file.write_text(json.dumps(self.config))
        (root / "evalve").mkdir(exist_ok=True)
        backend_env = root / "evalve-backend" / "backend.env"
        if backend_running:
            backend_env.parent.mkdir(exist_ok=True)
        with (
            patch.object(
                refresh, "get_secret", side_effect=self.secrets(database=database)
            ) as read,
            patch.object(refresh, "CONFIG", config_file),
            patch.object(refresh, "SERVER_ENV", root / "evalve" / "server.env"),
            patch.object(refresh, "BACKEND_ENV", backend_env),
        ):
            refresh.main(["--restart-if-changed"])
        return read, backend_env

    def test_timer_refreshes_the_backend_only_while_it_runs(self):
        for backend_running in (False, True):
            with (
                self.subTest(backend_running=backend_running),
                tempfile.TemporaryDirectory() as directory,
                patch.object(refresh.subprocess, "run"),
            ):
                root = Path(directory)
                read, backend_env = self.run_timer(root, backend_running)
                self.assertTrue((root / "evalve" / "server.env").exists())
                self.assertEqual(backend_env.exists(), backend_running)
                # Application and Database for Kitaru, plus Backend while it runs.
                self.assertEqual(read.call_count, 3 if backend_running else 2)

    def test_a_new_master_password_restarts_kitaru_but_not_the_backend(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh.subprocess, "run") as run,
        ):
            root = Path(directory)
            self.run_timer(root, backend_running=True)  # First run writes both files.
            run.reset_mock()
            self.run_timer(root, backend_running=True)
            run.assert_not_called()
            rotated = dict(self.database, password="rotated-master")
            self.run_timer(root, backend_running=True, database=rotated)
        run.assert_called_once_with(
            ["systemctl", "try-restart", "evalve-kitaru"], check=True
        )

    def start(self, root, calls, **docker):
        config_file = root / "config.json"
        config_file.write_text(json.dumps(self.config))
        backend_env = root / "evalve-backend" / "backend.env"
        backend_env.parent.mkdir()

        def run(command, **options):
            if command[:2] == ["docker", "run"]:
                # Setup runs before the backend gets its login.
                self.assertFalse(backend_env.exists())
            return self.docker(calls, **docker)(command, **options)

        with (
            patch.object(refresh, "get_secret", side_effect=self.secrets()) as read,
            patch.object(refresh.subprocess, "run", side_effect=run),
            patch.dict(refresh.os.environ, {"EVALVE_BACKEND_IMAGE": self.image}),
            patch.object(refresh, "CONFIG", config_file),
            patch.object(refresh, "SERVER_ENV", root / "evalve" / "server.env"),
            patch.object(refresh, "BACKEND_ENV", backend_env),
        ):
            refresh.main(["--backend"])
        return read, backend_env

    def test_backend_start_runs_setup_then_writes_only_the_backend_file(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            read, backend_env = self.start(root, calls)
            self.assertTrue(backend_env.exists())
            self.assertFalse((root / "evalve" / "server.env").exists())
        self.assertEqual(
            [call.args for call in read.call_args_list],
            [("db-secret", "us-east-1"), ("backend-secret", "us-east-1")],
        )
        self.assertEqual(
            [command[:2] for command, _ in calls],
            [["docker", "rm"], ["docker", "run"]],
        )

    def test_setup_command_holds_only_names_and_docker_passes_the_values(self):
        calls = []
        values = refresh.build_setup_environment(
            self.config, self.database, self.backend
        )
        with patch.object(refresh.subprocess, "run", side_effect=self.docker(calls)):
            refresh.run_setup(self.image, values)
        command, options = calls[-1]
        for secret in ("master-password", "role-password"):
            self.assertFalse(any(secret in argument for argument in command))
        self.assertEqual(
            {k: v for k, v in options["env"].items() if k != "PATH"}, values
        )
        for name in values:
            self.assertIn(["--env", name], [command[i : i + 2] for i in range(len(command))])
        self.assertEqual(
            command[:5], ["docker", "run", "--rm", "--name", "evalve-backend-setup"]
        )
        self.assertIn(
            "type=bind,src=/etc/evalve/rds-ca.pem,dst=/run/rds-ca.pem,readonly", command
        )
        # Docker's default network: the container can't reach the instance role.
        self.assertFalse(any(argument.startswith("--network") for argument in command))
        self.assertEqual(
            command[-5:],
            [self.image, "python", "-m", "evalve_backend", "setup-database"],
        )
        self.assertEqual(options["timeout"], 60)

    def test_setup_gets_both_logins_and_verified_tls(self):
        values = refresh.build_setup_environment(
            self.config, self.database, self.backend
        )
        self.assertEqual(
            values,
            {
                "EVALVE_DB_HOST": "db.example.com",
                "EVALVE_DB_PORT": "5432",
                "EVALVE_DB_ADMIN_USER": "evalve_admin",
                "EVALVE_DB_ADMIN_PWD": "master-password",
                "EVALVE_DB_USER": "evalve",
                "EVALVE_DB_PWD": "role-password",
                "EVALVE_DB_NAME": "evalve",
                "EVALVE_KITARU_DB_NAME": "kitaru",
                "EVALVE_DB_SSL_MODE": "verify-full",
                "EVALVE_DB_SSL_CA": "/run/rds-ca.pem",
            },
        )

    def test_failed_setup_writes_no_file_and_names_no_secret(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(refresh.SetupFailed) as failure:
                self.start(root, calls, returncode=1)
            self.assertFalse((root / "evalve-backend" / "backend.env").exists())
        message = str(failure.exception)
        self.assertIn("exit code 1", message)
        for secret in ("master-password", "role-password", "evalve_admin"):
            self.assertNotIn(secret, message)

    def test_slow_setup_is_removed_and_writes_no_file(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(refresh.SetupFailed, "within 60 seconds"):
                self.start(root, calls, timeout=True)
            self.assertFalse((root / "evalve-backend" / "backend.env").exists())
        # The container keeps running when the docker client stops, so it's removed.
        self.assertEqual(
            calls[-1][0], ["docker", "rm", "-f", "evalve-backend-setup"]
        )

    def test_missing_backend_secret_fails_before_reading_any_secret(self):
        config = {k: v for k, v in self.config.items() if k != "backend_secret_arn"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret") as read,
            patch.object(refresh.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(refresh.SetupFailed, "no backend secret"):
                refresh.start_backend(config, Path(directory) / "backend.env", self.image)
        read.assert_not_called()
        run.assert_not_called()

    def test_missing_image_runs_nothing(self):
        values = refresh.build_setup_environment(
            self.config, self.database, self.backend
        )
        with patch.object(refresh.subprocess, "run") as run:
            with self.assertRaisesRegex(refresh.SetupFailed, "image is not set"):
                refresh.run_setup("", values)
        run.assert_not_called()
