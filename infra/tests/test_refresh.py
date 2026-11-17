import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra import refresh


class RefreshTest(unittest.TestCase):
    config = {
        "application_secret_arn": "app-secret",
        "database_secret_arn": "db-secret",
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

    def test_backend_gets_only_the_database_login_in_a_private_file(self):
        database = {"username": "evalve_admin", "password": "db-password"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret", return_value=database) as read,
        ):
            destination = Path(directory) / "backend.env"
            refresh.refresh_backend(self.config, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(
                destination.read_text(),
                "EVALVE_DB_HOST=db.example.com\n"
                "EVALVE_DB_PORT=5432\n"
                "EVALVE_DB_USER=evalve_admin\n"
                "EVALVE_DB_PWD=db-password\n",
            )
        read.assert_called_once_with("db-secret", "us-east-1")

    def test_rotated_password_restarts_the_backend(self):
        database = {"username": "evalve_admin", "password": "old"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret", return_value=database),
            patch.object(refresh.subprocess, "run") as run,
        ):
            destination = Path(directory) / "backend.env"
            refresh.refresh_backend(self.config, destination)
            refresh.refresh_backend(self.config, destination, restart=True)
            run.assert_not_called()

            database["password"] = "rotated"
            refresh.refresh_backend(self.config, destination, restart=True)
            run.assert_called_once_with(
                ["systemctl", "try-restart", "evalve-backend"], check=True
            )
            self.assertIn("EVALVE_DB_PWD=rotated\n", destination.read_text())

    def test_backend_secret_cannot_add_environment_variables(self):
        database = {"username": "evalve_admin", "password": "unsafe\nINJECTED=value"}
        with self.assertRaises(ValueError):
            refresh.build_backend_environment(self.config, database)

    def test_timer_refreshes_the_backend_only_while_it_runs_and_reads_once(self):
        database = {"username": "evalve_admin", "password": "db-password"}
        secrets = {"app-secret": self.application, "db-secret": database}
        for backend_running in (False, True):
            with (
                self.subTest(backend_running=backend_running),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    refresh, "get_secret", side_effect=lambda arn, region: secrets[arn]
                ) as read,
                patch.object(refresh.subprocess, "run"),
            ):
                root = Path(directory)
                config_file = root / "config.json"
                config_file.write_text(json.dumps(self.config))
                (root / "evalve").mkdir()
                backend_env = root / "evalve-backend" / "backend.env"
                if backend_running:
                    backend_env.parent.mkdir()
                with (
                    patch.object(refresh, "CONFIG", config_file),
                    patch.object(refresh, "SERVER_ENV", root / "evalve" / "server.env"),
                    patch.object(refresh, "BACKEND_ENV", backend_env),
                ):
                    refresh.main(["--restart-if-changed"])
                self.assertTrue((root / "evalve" / "server.env").exists())
                self.assertEqual(backend_env.exists(), backend_running)
                self.assertEqual(read.call_count, 2)

    def test_backend_start_writes_only_the_backend_file(self):
        database = {"username": "evalve_admin", "password": "db-password"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(refresh, "get_secret", return_value=database) as read,
        ):
            root = Path(directory)
            config_file = root / "config.json"
            config_file.write_text(json.dumps(self.config))
            backend_env = root / "evalve-backend" / "backend.env"
            backend_env.parent.mkdir()
            with (
                patch.object(refresh, "CONFIG", config_file),
                patch.object(refresh, "SERVER_ENV", root / "evalve" / "server.env"),
                patch.object(refresh, "BACKEND_ENV", backend_env),
            ):
                refresh.main(["--backend"])
            self.assertTrue(backend_env.exists())
            self.assertFalse((root / "evalve" / "server.env").exists())
        read.assert_called_once_with("db-secret", "us-east-1")
