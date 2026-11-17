"""Fetch Kitaru's and the Evalve backend's credentials on EC2 using the instance role."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

CONFIG = Path("/etc/evalve/config.json")
SERVER_ENV = Path("/run/evalve/server.env")
BACKEND_ENV = Path("/run/evalve-backend/backend.env")


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


def build_backend_environment(config, database):
    # For this epic the backend uses Kitaru's PostgreSQL user (ADR 0004).
    return env_lines(
        {
            "EVALVE_DB_HOST": config["database_host"],
            "EVALVE_DB_PORT": "5432",
            "EVALVE_DB_USER": database["username"],
            "EVALVE_DB_PWD": database["password"],
        }
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


def refresh_backend(config, destination, restart=False, database=None):
    if database is None:
        database = get_secret(config["database_secret_arn"], config["region"])
    content = build_backend_environment(config, database)
    if write_if_changed(destination, content) and restart:
        subprocess.run(["systemctl", "try-restart", "evalve-backend"], check=True)


def main(argv):
    config = json.loads(CONFIG.read_text())
    restart = "--restart-if-changed" in argv
    if "--backend" in argv:
        refresh_backend(config, BACKEND_ENV)
        return
    database = refresh(config, SERVER_ENV, restart=restart)
    # The backend's runtime directory exists only while its unit runs.
    if restart and BACKEND_ENV.parent.is_dir():
        refresh_backend(config, BACKEND_ENV, restart=True, database=database)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        # Error details can include credentials. Keep them out of instance logs.
        print(
            "Credential refresh failed. Check IAM and AWS connectivity.",
            file=sys.stderr,
        )
        sys.exit(1)
