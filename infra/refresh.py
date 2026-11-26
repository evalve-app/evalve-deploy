"""Fetch Kitaru's and the Evalve backend's credentials on EC2 using the instance role."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

CONFIG = Path("/etc/evalve/config.json")
SERVER_ENV = Path("/run/evalve/server.env")
BACKEND_ENV = Path("/run/evalve-backend/backend.env")
RDS_CA = "/etc/evalve/rds-ca.pem"
SETUP_CONTAINER = "evalve-backend-setup"
SETUP_TIMEOUT = 60


class SetupFailed(Exception):
    """A failure of the database setup step. The message holds no secret."""


def get_secret(arn, region, *version_args):
    result = subprocess.run(
        [
            "aws",
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            arn,
            "--region",
            region,
            "--output",
            "json",
            *version_args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(json.loads(result.stdout)["SecretString"])


def env_lines(values):
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Environment values cannot contain line breaks")
    return "".join(f"{key}={value}\n" for key, value in values.items())


def build_environment(config, application, database):
    url = "https://" + config["hostname"]
    return env_lines(
        {
            "KITARU_SERVER_DB_HOST": config["database_host"],
            "KITARU_SERVER_DB_PORT": "5432",
            "KITARU_SERVER_DB_USER": database["username"],
            "KITARU_SERVER_DB_PWD": database["password"],
            "KITARU_SERVER_DB_NAME": "kitaru",
            "KITARU_SERVER_DB_SSL_MODE": "verify-full",
            "KITARU_SERVER_DB_SSL_CA": "/run/rds-ca.pem",
            "KITARU_SERVER_AUTH_SCHEME": "local",
            "KITARU_SERVER_JWT_SIGNING_KEY": application["jwt_signing_key"],
            "KITARU_SERVER_SECRET_ENCRYPTION_KEY": application["encryption_key"],
            "KITARU_SERVER_DEFAULT_ACCOUNT_PASSWORD": application["admin_password"],
            "KITARU_SERVER_SERVER_URL": url,
            "KITARU_SERVER_AUTH_COOKIE_NAME": "evalve_session",
            "KITARU_SERVER_AUTH_COOKIE_SECURE": "true",
            "KITARU_SERVER_CORS_ALLOW_ORIGINS": url,
            "KITARU_SERVER_ANALYTICS_OPT_IN": "false",
        }
    )


def build_backend_environment(config, backend):
    # The running backend gets only its own role, never the master user
    # (ADR 0005 point 2).
    return env_lines(
        {
            "EVALVE_DB_HOST": config["database_host"],
            "EVALVE_DB_PORT": "5432",
            "EVALVE_DB_USER": backend["db_user"],
            "EVALVE_DB_PWD": backend["db_password"],
        }
    )


def build_setup_environment(config, database, backend):
    values = {
        "EVALVE_DB_HOST": config["database_host"],
        "EVALVE_DB_PORT": "5432",
        "EVALVE_DB_ADMIN_USER": database["username"],
        "EVALVE_DB_ADMIN_PWD": database["password"],
        "EVALVE_DB_USER": backend["db_user"],
        "EVALVE_DB_PWD": backend["db_password"],
        "EVALVE_DB_NAME": "evalve",
        "EVALVE_KITARU_DB_NAME": "kitaru",
        "EVALVE_DB_SSL_MODE": "verify-full",
        "EVALVE_DB_SSL_CA": "/run/rds-ca.pem",
    }
    env_lines(values)  # The same line-break check as the env files.
    return values


def backend_secret_arn(config):
    arn = config.get("backend_secret_arn")
    if not arn:
        raise SetupFailed("The server config names no backend secret. Run evalve up.")
    return arn


def remove_setup_container():
    subprocess.run(
        ["docker", "rm", "-f", SETUP_CONTAINER], capture_output=True, timeout=30
    )


def run_setup(image, values):
    """Run the backend image once with setup-database (ADR 0005 point 6).

    The values reach the container through Docker's own environment: the
    command line holds only their names. The container uses Docker's default
    network, so it reaches RDS but not the instance role (IMDS hop limit 1).
    Setup prints only what it changed, so its output can go to the journal.
    """
    if not image:
        raise SetupFailed("The backend image is not set.")
    remove_setup_container()  # A crash can leave one behind.
    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        SETUP_CONTAINER,
        "--mount",
        f"type=bind,src={RDS_CA},dst=/run/rds-ca.pem,readonly",
        *[argument for name in values for argument in ("--env", name)],
        image,
        "python",
        "-m",
        "evalve_backend",
        "setup-database",
    ]
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **values}
    try:
        result = subprocess.run(command, env=environment, timeout=SETUP_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Stopping the docker client leaves the container running as the
        # master user, so remove it too.
        remove_setup_container()
        raise SetupFailed(
            f"The database setup did not finish within {SETUP_TIMEOUT} seconds."
        ) from None
    if result.returncode != 0:
        raise SetupFailed(
            f"The database setup failed (exit code {result.returncode})."
        )


def write_if_changed(destination, content):
    if destination.exists() and destination.read_text() == content:
        return False

    # The systemd unit creates a root-only runtime directory. Write the
    # replacement atomically so Docker cannot read a partially written file.
    with NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as output:
        output.write(content)
    Path(output.name).replace(destination)
    return True


def refresh(config, destination, restart=False):
    version = config.get("application_secret_version")
    version_args = ("--version-id", version) if version else ()
    application = get_secret(
        config["application_secret_arn"], config["region"], *version_args
    )
    database = get_secret(config["database_secret_arn"], config["region"])
    content = build_environment(config, application, database)
    if write_if_changed(destination, content) and restart:
        subprocess.run(["systemctl", "try-restart", "evalve-kitaru"], check=True)
    return database


def start_backend(config, destination, image):
    """The backend's prep step: set up the database, then write the role login."""
    arn = backend_secret_arn(config)  # Fail before reading any secret.
    database = get_secret(config["database_secret_arn"], config["region"])
    backend = get_secret(arn, config["region"])
    run_setup(image, build_setup_environment(config, database, backend))
    write_if_changed(destination, build_backend_environment(config, backend))


def refresh_backend(config, destination, restart=False):
    """The timer's check. A new backend password restarts the backend, and its
    prep step then runs setup, which sets the password in PostgreSQL."""
    backend = get_secret(backend_secret_arn(config), config["region"])
    content = build_backend_environment(config, backend)
    if write_if_changed(destination, content) and restart:
        subprocess.run(["systemctl", "try-restart", "evalve-backend"], check=True)


def main(argv):
    config = json.loads(CONFIG.read_text())
    restart = "--restart-if-changed" in argv
    if "--backend" in argv:
        # The backend unit loads the image from /etc/evalve/backend-image.env.
        start_backend(config, BACKEND_ENV, os.environ.get("EVALVE_BACKEND_IMAGE", ""))
        return
    refresh(config, SERVER_ENV, restart=restart)
    # The backend's runtime directory exists only while its unit runs.
    if restart and BACKEND_ENV.parent.is_dir():
        refresh_backend(config, BACKEND_ENV, restart=True)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except SetupFailed as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        # Error details can include credentials. Keep them out of instance logs.
        print(
            "Credential refresh failed. Check IAM and AWS connectivity.",
            file=sys.stderr,
        )
        sys.exit(1)
