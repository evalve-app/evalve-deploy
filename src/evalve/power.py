"""Graceful power changes for the EC2 instance already in Terraform state."""

import json
import time

from evalve import EvalveError
from evalve.aws import Aws, AwsError

# The backend unit exists only when the deployment has a backend_image. It
# stops before Kitaru, so it is not in the middle of a call to Kitaru, and it
# has finished its writes before down takes the database snapshot.
BACKEND_UNIT = "evalve-backend.service"
IF_BACKEND = f"if systemctl cat {BACKEND_UNIT} >/dev/null 2>&1; then"
DRAIN = [
    "systemctl stop evalve-refresh.timer evalve-refresh.service",
    f"{IF_BACKEND} systemctl stop {BACKEND_UNIT}; fi",
    "running=$(docker ps --filter name=^/evalve-backend$ --format '{{.ID}}')",
    'test -z "$running"',
    "systemctl stop evalve-kitaru.service",
    "running=$(docker ps --filter name=^/evalve-kitaru$ --format '{{.ID}}')",
    'test -z "$running"',
]
BACKEND_IMAGE_FILE = "/etc/evalve/backend-image.env"


def backend_deploy_commands(image: str) -> list[str]:
    """Write the backend image on EC2 and restart only the backend when it changed.

    The image is not in user_data, so a new image does not replace EC2. The CLI
    has already checked that the image is a digest, so it is safe in the shell.
    """
    line = f"EVALVE_BACKEND_IMAGE={image}"
    return [
        # On a new server, wait until the bootstrap has written the unit.
        "cloud-init status --wait",
        f"if [ \"$(cat {BACKEND_IMAGE_FILE} 2>/dev/null)\" != '{line}' ]; then"
        f" printf '%s\\n' '{line}' > {BACKEND_IMAGE_FILE}.new"
        f" && mv {BACKEND_IMAGE_FILE}.new {BACKEND_IMAGE_FILE}"
        f" && systemctl restart {BACKEND_UNIT};"
        f" else systemctl start {BACKEND_UNIT}; fi",
    ]


def deploy_backend(aws: Aws, instance_id: str, image: str) -> None:
    run_server_command(aws, instance_id, backend_deploy_commands(image))


# Caddy mounts this folder and serves its `current` link. Each release unpacks
# into releases/<sha256>, and `current` switches to it in one rename.
DASHBOARD_DIR = "/etc/evalve/dashboard"


def dashboard_install_script(url: str, sha256: str, root: str = DASHBOARD_DIR) -> str:
    """Install the dashboard bundle under root and point `current` at it.

    The download is checked against sha256 before anything changes, so a bad
    download leaves the served dashboard as it was. A release that is already
    unpacked is not downloaded again. The link is relative because Caddy sees
    the folder at another path. The previous release stays for a rollback, and
    older ones are removed. Caddy reads the files on each request, so it needs
    no restart. The CLI pins url and sha256 as constants that tests check, so
    they are safe in the shell.
    """
    release = f"releases/{sha256}"
    return (
        f"d='{root}'; r=\"$d/{release}\";"
        ' prev=$(readlink "$d/current" 2>/dev/null || true);'
        ' if [ ! -f "$r/index.html" ]; then'
        ' tmp=$(mktemp -d "$d/.tmp-XXXXXX")'
        f" && curl -fsSL --retry 3 -o \"$tmp/dashboard.tgz\" '{url}'"
        f" && echo '{sha256}  '\"$tmp/dashboard.tgz\" | sha256sum --check --quiet"
        ' && mkdir "$tmp/site" && tar -xzf "$tmp/dashboard.tgz" -C "$tmp/site"'
        ' && test -f "$tmp/site/index.html"'
        ' && install -d "$d/releases" && rm -rf "$r" && mv "$tmp/site" "$r";'
        ' rc=$?; rm -rf "$tmp"; [ "$rc" -eq 0 ] || exit "$rc";'
        " fi;"
        f" ln -sfn '{release}' \"$d/.current.new\""
        ' && mv -T "$d/.current.new" "$d/current" || exit 1;'
        ' for old in "$d"/releases/*; do'
        ' case "$old" in "$r"|"$d/$prev") ;; *) rm -rf "$old" ;; esac;'
        " done"
    )


def deploy_dashboard(aws: Aws, instance_id: str, url: str, sha256: str) -> None:
    run_server_command(
        aws,
        instance_id,
        # On a new server, wait until the bootstrap has made the folder.
        ["cloud-init status --wait", dashboard_install_script(url, sha256)],
    )


START = [
    "cloud-init status --wait",
    "systemctl start evalve-kitaru.service evalve-refresh.timer evalve-caddy.service",
    f"{IF_BACKEND} systemctl start {BACKEND_UNIT}; fi",
]


def instance_state(aws: Aws, instance_id: str, *, allow_gone: bool = False) -> str:
    try:
        result = aws.call("ec2", "describe-instances", "--instance-ids", instance_id)
    except AwsError as exc:
        if allow_gone and exc.code == "InvalidInstanceID.NotFound":
            return "terminated"
        raise
    instances = [
        item
        for reservation in result["Reservations"]
        for item in reservation["Instances"]
    ]
    if len(instances) != 1 or instances[0]["InstanceId"] != instance_id:
        raise EvalveError("The recorded EC2 instance could not be verified.")
    instance = instances[0]
    if {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}.get(
        "ManagedBy"
    ) != "evalve":
        raise EvalveError(
            "The recorded EC2 instance does not have the Evalve ownership tag."
        )
    return instance["State"]["Name"]


def run_server_command(aws: Aws, instance_id: str, commands: list[str]) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = aws.call(
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        )
        if any(
            item["InstanceId"] == instance_id and item["PingStatus"] == "Online"
            for item in result["InstanceInformationList"]
        ):
            break
        time.sleep(5)
    else:
        raise EvalveError(
            "EC2 is not online in Systems Manager. Check instance IAM, networking, and bootstrap logs."
        )

    command_id = aws.call(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps({"commands": ["set -eu", *commands], "executionTimeout": ["240"]}),
        "--timeout-seconds",
        "60",
    )["Command"]["CommandId"]
    deadline = time.monotonic() + 320
    while time.monotonic() < deadline:
        try:
            status = aws.call(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )["Status"]
        except AwsError as exc:
            if exc.code != "InvocationDoesNotExist":
                raise
        else:
            if status == "Success":
                return
            if status not in {"Pending", "InProgress", "Delayed"}:
                raise EvalveError(
                    f"Systems Manager command {command_id} ended with status {status}. Check its details and retry."
                )
        time.sleep(3)
    raise EvalveError(
        f"Systems Manager command {command_id} timed out. Check its status before retrying."
    )


def start(aws: Aws, instance_id: str) -> None:
    state = instance_state(aws, instance_id)
    if state == "stopping":
        aws.call("ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
        state = "stopped"
    if state == "stopped":
        aws.call("ec2", "start-instances", "--instance-ids", instance_id)
    elif state not in {"running", "pending"}:
        raise EvalveError(f"Cannot start the recorded EC2 instance in state {state}.")
    if state != "running":
        aws.call("ec2", "wait", "instance-running", "--instance-ids", instance_id)
    run_server_command(aws, instance_id, START)


def stop(aws: Aws, instance_id: str, *, allow_gone: bool = False) -> None:
    state = instance_state(aws, instance_id, allow_gone=allow_gone)
    if state == "stopped" or (allow_gone and state == "terminated"):
        return
    if state == "pending":
        aws.call("ec2", "wait", "instance-running", "--instance-ids", instance_id)
        state = "running"
    if state == "running":
        run_server_command(aws, instance_id, DRAIN)
        aws.call("ec2", "stop-instances", "--instance-ids", instance_id)
    elif state != "stopping":
        raise EvalveError(f"Cannot stop the recorded EC2 instance in state {state}.")
    aws.call("ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
