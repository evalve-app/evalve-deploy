"""Evalve's operator commands."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import subprocess
import sys
from decimal import Decimal

from evalve import EvalveError, lifecycle, power, terraform
from evalve.aws import Aws, read_application_secret
from evalve.readiness import wait_for_backend, wait_for_dashboard, wait_for_kitaru

# Bump this to the digest of each evalve-backend release. Deployments that
DEFAULT_BACKEND_IMAGE = (
    "ghcr.io/evalve-app/evalve-backend@sha256:"
    "f14f842d1bbd3f3d75188c7cd0a01af94da579d056dba6d9a25fd476c2192766"
)
# Bump both to a new evalve-ui release. Its Release workflow summary prints them.
DEFAULT_DASHBOARD_URL = (
    "https://github.com/evalve-app/evalve-ui/releases/download/v0.1.2/dashboard.tgz"
)
DEFAULT_DASHBOARD_SHA256 = "395355dbbc3e91fe118dff204f6acfb5d94057cbdc839a764592346f6127d348"


def monthly_estimate(value: str) -> Decimal:
    value = value.strip()
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value) or Decimal(value) <= 0:
        raise argparse.ArgumentTypeError(
            "Enter a positive monthly estimate in USD before credits, such as 32.50."
        )
    return Decimal(value)


def hostname(value: str) -> str:
    if len(value) > 253 or not re.fullmatch(
        r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", value
    ):
        raise argparse.ArgumentTypeError(
            "Use a lowercase DNS hostname such as kitaru.example.com."
        )
    return value


def backend_image(value: str) -> str:
    if value == "default":
        return value
    if value and not re.fullmatch(
        r"ghcr\.io/evalve-app/evalve-backend@sha256:[a-f0-9]{64}", value
    ):
        raise argparse.ArgumentTypeError(
            "Use an Evalve backend image digest such as "
            "ghcr.io/evalve-app/evalve-backend@sha256:<64 hex characters>, "
            '"default" to follow the repo default, or an empty value to remove the backend.'
        )
    return value


def choose_backend(flag: str | None, existing: dict) -> tuple[str, bool]:
    """Return the backend image for this up and whether an Operator pinned it."""
    if flag == "default":
        return DEFAULT_BACKEND_IMAGE, False
    if flag is not None:  # a digest, or "" to remove the backend
        return flag, True
    if not existing:  # first deployment
        return DEFAULT_BACKEND_IMAGE, False
    saved = existing.get("backend_image", "")
    pinned = existing.get("backend_image_pinned", False)
    if pinned or not saved:
        # Keep a hand-picked image, and never turn a backend on silently,
        # because that replaces EC2.
        return saved, pinned
    return DEFAULT_BACKEND_IMAGE, False


def backup_retention_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Backup retention must be a whole number between 1 and 35 days."
        ) from exc
    if not 1 <= days <= 35:
        raise argparse.ArgumentTypeError(
            "Backup retention must be between 1 and 35 days."
        )
    return days


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Deploy a shared Kitaru server on AWS."
    )
    commands = result.add_subparsers(dest="command", required=True)
    up = commands.add_parser("up", help="Create or update the shared deployment.")
    for command in (
        up,
        commands.add_parser("start", help="Start the existing EC2 server."),
        commands.add_parser(
            "stop", help="Drain and stop EC2 while keeping RDS running."
        ),
        commands.add_parser(
            "down", help="Save a verified database snapshot and remove the deployment."
        ),
        commands.add_parser(
            "credentials", help="Print the bootstrap Admin account login."
        ),
    ):
        command.add_argument(
            "--profile", default="evalve", help="AWS CLI profile (default: evalve)."
        )
        command.add_argument(
            "--region", help="AWS region (defaults to the profile's configured region)."
        )
    up.add_argument(
        "--server-type", help="x86-64 EC2 instance type. Required for the first apply."
    )
    up.add_argument(
        "--database-class",
        help="RDS PostgreSQL instance class. Required for the first apply.",
    )
    up.add_argument(
        "--backup-retention-days",
        type=backup_retention_days,
        help="RDS automated backup retention (default: 7 days; use 1 for the Free plan pilot).",
    )
    mode = up.add_mutually_exclusive_group()
    mode.add_argument(
        "--domain",
        type=hostname,
        help="Switch an existing deployment to this hostname.",
    )
    mode.add_argument(
        "--restore",
        metavar="SNAPSHOT_ID",
        help="Restore the selected Evalve snapshot into an empty deployment.",
    )
    up.add_argument(
        "--backend-image",
        type=backend_image,
        help="Evalve backend image digest to pin. Without the flag, up uses the repo "
        'default. Pass "default" to follow the repo again, or "" to remove the backend.',
    )
    up.add_argument(
        "--monthly-estimate",
        type=monthly_estimate,
        help="Reviewed AWS estimate in USD before credits.",
    )
    return result


def check_tools(needs_terraform: bool = True) -> None:
    for program in ("aws", "terraform") if needs_terraform else ("aws",):
        if shutil.which(program) is None:
            raise EvalveError(
                f"Install {program} and put it on PATH before running Evalve."
            )
    if not needs_terraform:
        return
    result = subprocess.run(
        ["terraform", "version", "-json"], capture_output=True, text=True
    )
    version = (
        json.loads(result.stdout).get("terraform_version", "")
        if result.returncode == 0
        else ""
    )
    match = re.match(r"^1\.(\d+)\.", version)
    if not match or int(match[1]) < 11:
        raise EvalveError(
            "Install Terraform 1.11 or newer in the 1.x series before running up."
        )


def verify_dns(domain: str, public_ip: str) -> None:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise EvalveError(
            f"DNS for {domain} is unavailable. Point its A record at {public_ip} and retry."
        ) from exc
    if addresses != {public_ip}:
        raise EvalveError(
            f"DNS for {domain} must point only to {public_ip}. Update DNS and retry."
        )


def up(args: argparse.Namespace) -> None:
    check_tools()
    aws = Aws(args.profile, args.region)
    with terraform.operation_directory(aws) as directory:
        operation, _ = lifecycle.pending_operation(aws)
        outputs = aws.state_outputs()
        if args.restore:
            existing = lifecycle.load_restore(aws, args.restore, operation)
        else:
            lifecycle.require_idle(operation)
            existing = outputs.get("deployment", {})
            instance_id = outputs.get("instance_id")
            if instance_id and power.instance_state(
                aws, instance_id, allow_gone=True
            ) in {"stopped", "stopping"}:
                raise EvalveError(
                    "The existing EC2 server is stopped or stopping. Run evalve start first."
                )
        server_type = args.server_type or existing.get("server_instance_type")
        database_class = args.database_class or existing.get("database_instance_class")
        retention_days = args.backup_retention_days or existing.get(
            "backup_retention_days", 7
        )
        if not server_type or not database_class:
            raise EvalveError(
                "For the first deployment, pass --server-type and --database-class."
            )
        if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", server_type) or not re.fullmatch(
            r"db\.[a-z0-9-]+\.[a-z0-9-]+", database_class
        ):
            raise EvalveError(
                "Use an EC2 type such as t3.small and an RDS class such as db.t4g.micro."
            )
        aws.verify_server_type(server_type)
        domain = args.domain or existing.get("domain")
        if domain:
            public_ip = outputs.get("public_ip")
            if not public_ip:
                raise EvalveError(
                    "Run up with the default sslip.io address before switching to a custom domain."
                )
            verify_dns(domain, public_ip)
            verify_dns(f"dashboard.{domain}", public_ip)

        print(
            f"AWS profile: {aws.profile} | Account: {aws.account} | Region: {aws.region}"
        )
        print(
            f"EC2: {server_type} with 20 GiB gp3 | RDS: {database_class} single-AZ with 20 GiB gp3"
        )
        print(f"RDS automated backup retention: {retention_days} day(s)")
        print(
            f"Endpoint: {domain or 'sslip.io'} on one Elastic IP | State bucket: {aws.bucket}"
        )
        saved_backend = existing.get("backend_image", "")
        backend, pinned = choose_backend(args.backend_image, existing)
        print(f"Evalve backend: {backend or 'none'}{' (pinned)' if pinned else ''}")
        if backend and pinned and backend != DEFAULT_BACKEND_IMAGE:
            print(
                "The pinned backend image must be v0.3.0 or newer. The server runs "
                "its setup-database step before each start, and older images lack it."
            )
        print(f"Evalve dashboard: {DEFAULT_DASHBOARD_URL}")
        if outputs.get("instance_id") and not args.restore:
            if bool(backend) != bool(saved_backend):
                print(
                    "Turning the backend on or off replaces the EC2 server. "
                    "The URL and the RDS data stay."
                )
            elif backend != saved_backend:
                print("The new backend image restarts only the backend container.")
        estimate = args.monthly_estimate
        if estimate is None:
            print(
                "Include compute, storage, the Elastic IP, backups, secrets, and S3 in the estimate."
            )
            estimate = monthly_estimate(
                input("Reviewed monthly AWS estimate in USD before credits: ")
            )
        print(
            f"Operator estimate: ${estimate:,.2f}/month before credits or allowances."
        )
        if input(
            f"Apply Evalve in account {aws.account}? [y/N] "
        ).strip().lower() not in {"y", "yes"}:
            print("Deployment cancelled.")
            return
        lifecycle.verify_account(aws)
        if not args.restore:
            lifecycle.require_idle(lifecycle.pending_operation(aws)[0])

        aws.ensure_bucket()
        secret = aws.ensure_application_secret(existing.get("application_secret_arn"))
        # With no backend, keep the saved ARN, so a later backend reuses the
        # same secret and password (ADR 0005 point 7).
        backend_secret = existing.get("backend_secret_arn") or ""
        if backend:
            backend_secret = aws.ensure_backend_secret(backend_secret or None)
        restore_etag = (
            lifecycle.record_restore(aws, args.restore, operation)
            if args.restore
            else None
        )
        outputs = terraform.apply(
            aws,
            directory,
            {
                "account_id": aws.account,
                "region": aws.region,
                "server_instance_type": server_type,
                "database_instance_class": database_class,
                "backup_retention_days": retention_days,
                "application_secret_arn": secret,
                "backend_secret_arn": backend_secret,
                "domain": domain,
                **{
                    key: existing[key]
                    for key in lifecycle.TF_SETTINGS
                    if key in existing
                },
                "backend_image": backend,
                "backend_image_pinned": pinned,
            },
        )
        url = outputs["url"]
        print("Waiting for HTTPS, database health, and API authentication…", flush=True)
        wait_for_kitaru(url)
        # A backend failure is reported after the dashboard, so Kitaru and the
        # dashboard still work when setup fails (ADR 0005 point 6).
        backend_error = None
        if backend:
            print("Deploying the Evalve backend…", flush=True)
            try:
                power.deploy_backend(aws, outputs["instance_id"], backend)
            except EvalveError as exc:
                backend_error = exc
        dashboard_url = outputs["dashboard_url"]
        print("Installing the Evalve dashboard…", flush=True)
        power.deploy_dashboard(
            aws, outputs["instance_id"], DEFAULT_DASHBOARD_URL, DEFAULT_DASHBOARD_SHA256
        )
        print("Waiting for the Evalve dashboard…", flush=True)
        wait_for_dashboard(dashboard_url)
        if backend and backend_error is None:
            print("Waiting for the Evalve backend…", flush=True)
            try:
                wait_for_backend(url, backend)
            except EvalveError as exc:
                backend_error = exc
        if backend_error:
            # The restore checkpoint stays, so up can be run again.
            raise EvalveError(
                f"The Evalve backend did not start ({backend_error}). Kitaru and "
                "the dashboard are running. On the server, `journalctl -u "
                "evalve-backend` shows why. Fix it and run evalve up again."
            )
        if restore_etag:
            aws.delete_document(lifecycle.OPERATION_KEY, restore_etag)
        print(f"Bundled dashboard: {url}")
        print(f"Evalve dashboard: {dashboard_url}")
        if backend:
            print(f"Evalve backend: {url}/evalve/health")
        print(
            "Run evalve credentials with the same AWS profile and region for the bootstrap Admin account login."
        )


def credentials(aws: Aws) -> None:
    deployment = aws.state_outputs().get("deployment", {})
    arn = deployment.get("application_secret_arn")
    if not arn:
        raise EvalveError(
            "No Evalve application secret is recorded for this account and region. Run up first."
        )
    secret, _ = read_application_secret(
        aws, arn, deployment.get("application_secret_version")
    )
    print(f"Username: default\nPassword: {secret['admin_password']}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "up":
            up(args)
        else:
            check_tools(needs_terraform=args.command == "down")
            aws = Aws(args.profile, args.region)
            if args.command == "credentials":
                credentials(aws)
            else:
                with terraform.operation_directory(aws) as directory:
                    if args.command == "down":
                        lifecycle.down(aws, directory)
                    else:
                        lifecycle.power_change(aws, args.command)
    except (
        EvalveError,
        argparse.ArgumentTypeError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"Evalve: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print(
            "\nEvalve: Command interrupted. Rerun the same command to resume.",
            file=sys.stderr,
        )
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
