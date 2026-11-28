"""Graceful power changes for the EC2 instance already in Terraform state."""

import json
import shlex
import subprocess
import sys
import time

from evalve import EvalveError
from evalve.aws import Aws, AwsError

# The backend unit exists only when the deployment has a backend_image. It
# stops before Kitaru, so it is not in the middle of a call to Kitaru, and it
# has finished its writes before down takes the database snapshot.
BACKEND_UNIT = "evalve-backend.service"
IF_BACKEND = f"if systemctl cat {BACKEND_UNIT} >/dev/null 2>&1; then"
DRAIN_BACKEND = [
    "systemctl stop evalve-refresh.timer evalve-refresh.service",
    f"{IF_BACKEND} systemctl stop {BACKEND_UNIT}; fi",
    "running=$(docker ps --filter name=^/evalve-backend$ --format '{{.ID}}')",
    'test -z "$running"',
]
DRAIN_KITARU = [
    "systemctl stop evalve-kitaru.service",
    "running=$(docker ps --filter name=^/evalve-kitaru$ --format '{{.ID}}')",
    'test -z "$running"',
]
DRAIN = DRAIN_BACKEND + DRAIN_KITARU
BACKEND_IMAGE_FILE = "/etc/evalve/backend-image.env"
BACKEND_SETTINGS_FILE = "/etc/evalve/backend-settings.env"


def backend_deploy_commands(image: str, settings: dict[str, str]) -> list[str]:
    """Install the settings and image on EC2, then start or restart the backend."""
    lines = " ".join(shlex.quote(f"{key}={value}") for key, value in settings.items())
    image_line = shlex.quote(f"EVALVE_BACKEND_IMAGE={image}")
    # The lock covers both files and the restart. The marker survives an
    # interrupted deploy so a retry applies changes already written to disk.
    install = f"""umask 077
exec 9>/etc/evalve/backend-deploy.lock
flock 9
restart_pending=/etc/evalve/backend-restart-pending
temporary=
trap 'if [ -n "$temporary" ]; then rm -f -- "$temporary"; fi' EXIT
install_env_file() {{
    destination=$1
    shift
    temporary=$(mktemp "$destination.XXXXXX")
    printf '%s\\n' "$@" > "$temporary"
    if ! cmp -s "$temporary" "$destination"; then
        touch "$restart_pending"
        mv -f -- "$temporary" "$destination"
    else
        rm -f -- "$temporary"
    fi
    temporary=
}}
install_env_file {BACKEND_SETTINGS_FILE} {lines}
install_env_file {BACKEND_IMAGE_FILE} {image_line}
if [ -f "$restart_pending" ]; then
    systemctl restart --no-block {BACKEND_UNIT}
    rm -f -- "$restart_pending"
else
    systemctl start --no-block {BACKEND_UNIT}
fi"""
    return [
        "cloud-init status --wait",
        # A failed pull leaves the files and running backend alone.
        f"docker pull {shlex.quote(image)}",
        install,
    ]


def deploy_backend(
    aws: Aws, instance_id: str, image: str, settings: dict[str, str]
) -> None:
    run_server_command(aws, instance_id, backend_deploy_commands(image, settings))


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
        # Already served: change nothing, so the previous release stays.
        f" if [ \"$prev\" = '{release}' ] && [ -f \"$r/index.html\" ]; then exit 0; fi;"
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


def describe_loop_tasks(aws: Aws, cluster: str, batch: list[str], **options) -> dict:
    result = aws.call(
        "ecs", "describe-tasks", "--cluster", cluster, "--tasks", *batch, **options
    )
    tasks = {task["taskArn"]: task for task in result["tasks"]}
    if tasks.keys() != set(batch):
        raise EvalveError("ECS could not verify the status of all Loop tasks.")
    return tasks


def request_loop_task_stops(
    aws: Aws, cluster: str | None, *, action: str
) -> set[str]:
    """Request shutdown and return the task ARNs that still need to be checked."""
    if not cluster:
        return set()

    # ListTasks filters by desired status. A task with desired status STOPPED
    # may still be active, including on a retry after an earlier drain.
    arns = set()
    for status in ("RUNNING", "STOPPED"):
        result = aws.call(
            "ecs", "list-tasks", "--cluster", cluster, "--desired-status", status
        )
        arns.update(result["taskArns"])

    ordered = sorted(arns)
    active = set()
    for offset in range(0, len(ordered), 100):
        batch = ordered[offset : offset + 100]
        tasks = describe_loop_tasks(aws, cluster, batch)
        for arn, task in tasks.items():
            if task["lastStatus"] == "STOPPED":
                continue
            if task["desiredStatus"] != "STOPPED":
                aws.call(
                    "ecs", "stop-task", "--cluster", cluster, "--task", arn,
                    "--reason", f"evalve {action}",
                )
            active.add(arn)
    return active


def wait_for_loop_tasks(aws: Aws, cluster: str, task_arns: set[str]) -> int:
    """Return how many watched tasks ECS confirmed stopped within 150 seconds."""
    remaining = set(task_arns)
    deadline = time.monotonic() + 150
    while remaining and time.monotonic() < deadline:
        ordered = sorted(remaining)
        for offset in range(0, len(ordered), 100):
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                tasks = describe_loop_tasks(
                    aws, cluster, ordered[offset : offset + 100], timeout=timeout
                )
            except subprocess.TimeoutExpired:
                # The status request used the remainder of the wait budget.
                break
            remaining.difference_update(
                arn for arn, task in tasks.items() if task["lastStatus"] == "STOPPED"
            )
        delay = deadline - time.monotonic()
        if remaining and delay > 0:
            time.sleep(min(5, delay))

    for arn in sorted(remaining):
        print(f"Warning: Loop task still active after 150 seconds: {arn}", file=sys.stderr)
    return len(task_arns) - len(remaining)


def drain_loop_tasks(aws: Aws, cluster: str, *, action: str) -> int:
    try:
        task_arns = request_loop_task_stops(aws, cluster, action=action)
        return wait_for_loop_tasks(aws, cluster, task_arns)
    except AwsError as exc:
        # A manually removed cluster may still be recorded in Terraform state.
        if action == "down" and exc.code == "ClusterNotFoundException":
            return 0
        raise


def stop(
    aws: Aws,
    instance_id: str,
    *,
    allow_gone: bool = False,
    cluster: str | None = None,
    action: str = "stop",
) -> int:
    state = instance_state(aws, instance_id, allow_gone=allow_gone)
    if state == "stopped" or (allow_gone and state == "terminated"):
        return drain_loop_tasks(aws, cluster, action=action) if cluster else 0
    if state == "pending":
        aws.call("ec2", "wait", "instance-running", "--instance-ids", instance_id)
        state = "running"

    stopped = 0
    if state == "running":
        if cluster:
            run_server_command(aws, instance_id, DRAIN_BACKEND)
            stopped = drain_loop_tasks(aws, cluster, action=action)
            run_server_command(aws, instance_id, DRAIN_KITARU)
        else:
            run_server_command(aws, instance_id, DRAIN)
        aws.call("ec2", "stop-instances", "--instance-ids", instance_id)
    elif state != "stopping":
        raise EvalveError(f"Cannot stop the recorded EC2 instance in state {state}.")
    aws.call("ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
    if state == "stopping" and cluster:
        return drain_loop_tasks(aws, cluster, action=action)
    return stopped
