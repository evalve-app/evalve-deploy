import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import DEFAULT, Mock, patch

from evalve import EvalveError, backup, cli, lifecycle, power, terraform
from evalve.aws import STATE_KEY, AwsError

ACCOUNT = "123456789012"
REGION = "us-east-1"
INSTANCE = "i-0123456789abcdef0"
DB = "db-0123456789ABCDEFG"
SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:evalve/application-test"
VERSION = "12345678-1234-1234-1234-123456789abc"
SNAPSHOT = "evalve-20261110170000-123456789abc"
CONFIG = {
    "server_instance_type": "t3.small",
    "database_instance_class": "db.t4g.micro",
    "backup_retention_days": 1,
    "application_secret_arn": SECRET,
    "application_secret_version": VERSION,
    "domain": "example.com",
}

FARGATE_OUTPUTS = {
    "ecs_cluster_name": "evalve",
    "loop_task_subnet_id": "subnet-0123456789",
    "loop_task_security_group_id": "sg-0123456789",
    "loop_task_execution_role_arn": f"arn:aws:iam::{ACCOUNT}:role/loop-execution",
    "loop_task_log_group": "/evalve/loop-tasks",
    "agent_image_repository_url": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/evalve",
    "agent_image_push_role_arn": f"arn:aws:iam::{ACCOUNT}:role/agent-push",
}
FARGATE_SETTINGS = {
    "EVALVE_EXECUTOR": "fargate",
    "EVALVE_AWS_REGION": REGION,
    "EVALVE_PUBLIC_URL": "https://example.sslip.io/evalve",
    "EVALVE_KITARU_PUBLIC_URL": "https://example.sslip.io",
    "EVALVE_ECS_CLUSTER": "evalve",
    "EVALVE_LOOP_TASK_SUBNET": "subnet-0123456789",
    "EVALVE_LOOP_TASK_SECURITY_GROUP": "sg-0123456789",
    "EVALVE_LOOP_TASK_EXECUTION_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/loop-execution",
    "EVALVE_LOOP_TASK_LOG_GROUP": "/evalve/loop-tasks",
    "EVALVE_AGENT_IMAGE_REPOSITORY": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/evalve",
    "EVALVE_AGENT_IMAGE_PUSH_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/agent-push",
}


def resource(kind, name, attributes):
    return {
        "mode": "managed",
        "type": kind,
        "name": name,
        "instances": [{"attributes": attributes}],
    }


class PowerTest(unittest.TestCase):
    def test_start_reuses_same_ec2_without_touching_database(self):
        aws = Mock()
        with (
            patch.object(power, "instance_state", return_value="stopped"),
            patch.object(power, "run_server_command") as command,
        ):
            power.start(aws, INSTANCE)
        self.assertEqual(
            [call.args for call in aws.call.call_args_list],
            [
                ("ec2", "start-instances", "--instance-ids", INSTANCE),
                ("ec2", "wait", "instance-running", "--instance-ids", INSTANCE),
            ],
        )
        command.assert_called_once_with(aws, INSTANCE, power.START)

    def test_start_checks_the_backend_after_kitaru_only_when_there_is_one(self):
        image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64
        url = "https://example.sslip.io"
        for backend in (image, None):
            with self.subTest(backend=backend):
                deployment = dict(CONFIG, backend_image=backend) if backend else CONFIG
                aws = Mock(account=ACCOUNT, region=REGION)
                aws.caller_account.return_value = ACCOUNT
                aws.state_outputs.return_value = {
                    "instance_id": INSTANCE,
                    "url": url,
                    "deployment": deployment,
                }
                calls = []
                with (
                    patch.object(
                        lifecycle, "pending_operation", return_value=(None, None)
                    ),
                    patch("builtins.input", return_value="y"),
                    redirect_stdout(io.StringIO()),
                    patch.object(power, "start"),
                    patch.object(
                        lifecycle,
                        "wait_for_kitaru",
                        side_effect=lambda *a: calls.append(("kitaru", a)),
                    ),
                    patch.object(
                        lifecycle,
                        "wait_for_backend",
                        side_effect=lambda *a: calls.append(("backend", a)),
                    ),
                ):
                    lifecycle.power_change(aws, "start")
                expected = [("kitaru", (url,))]
                if backend:
                    expected.append(("backend", (url, image)))
                self.assertEqual(calls, expected)

    def test_start_checks_the_dashboard_only_on_a_server_that_has_one(self):
        url = "https://example.sslip.io"
        dashboard = "https://dashboard.example.sslip.io"
        for dashboard_url in (dashboard, None):
            with self.subTest(dashboard_url=dashboard_url):
                aws = Mock(account=ACCOUNT, region=REGION)
                aws.caller_account.return_value = ACCOUNT
                outputs = {"instance_id": INSTANCE, "url": url, "deployment": CONFIG}
                if dashboard_url:
                    outputs["dashboard_url"] = dashboard_url
                aws.state_outputs.return_value = outputs
                with (
                    patch.object(
                        lifecycle, "pending_operation", return_value=(None, None)
                    ),
                    patch("builtins.input", return_value="y"),
                    redirect_stdout(io.StringIO()) as output,
                    patch.object(power, "start"),
                    patch.object(lifecycle, "wait_for_kitaru"),
                    patch.object(lifecycle, "wait_for_dashboard") as check,
                ):
                    lifecycle.power_change(aws, "start")
                if dashboard_url:
                    check.assert_called_once_with(dashboard)
                    self.assertIn(f"Evalve dashboard: {dashboard}", output.getvalue())
                else:
                    check.assert_not_called()
                    self.assertNotIn("Evalve dashboard", output.getvalue())

    def test_dashboard_deploy_waits_for_the_bootstrap_then_installs(self):
        url = "https://github.com/evalve-app/evalve-ui/releases/download/v9.9.9/dashboard.tgz"
        sha = "a" * 64
        aws = Mock()
        with patch.object(power, "run_server_command") as run:
            power.deploy_dashboard(aws, INSTANCE, url, sha)
        run.assert_called_once_with(
            aws,
            INSTANCE,
            ["cloud-init status --wait", power.dashboard_install_script(url, sha)],
        )
        self.assertIn("d='/etc/evalve/dashboard'", run.call_args.args[2][1])

    def test_backend_deploy_installs_files_and_restarts_only_on_change(self):
        image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64
        settings = FARGATE_SETTINGS
        commands = power.backend_deploy_commands(image, settings)
        self.assertEqual(commands[:2], ["cloud-init status --wait", f"docker pull {image}"])
        aws = Mock()
        with patch.object(power, "run_server_command") as run:
            power.deploy_backend(aws, INSTANCE, image, settings)
        run.assert_called_once_with(aws, INSTANCE, commands)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "backend-settings.env"
            image_file = root / "backend-image.env"
            restart_pending = root / "backend-restart-pending"

            def deploy(settings, fail_restart=False):
                script = power.backend_deploy_commands(image, settings)[2]
                self.assertLess(
                    script.index("install_env_file /etc/evalve/backend-settings.env"),
                    script.index("install_env_file /etc/evalve/backend-image.env"),
                )
                # Exercise the actual shell commands without touching the host service.
                systemctl = (
                    'systemctl() { printf "%s\\n" "$*"; return '
                    + ("1" if fail_restart else "0")
                    + "; }\n"
                )
                return subprocess.run(
                    ["sh", "-eu", "-c", systemctl + script.replace("/etc/evalve", directory)],
                    capture_output=True,
                    text=True,
                )

            restart = "restart --no-block evalve-backend.service\n"
            start = "start --no-block evalve-backend.service\n"
            self.assertEqual(deploy(settings).stdout, restart)
            self.assertEqual(
                settings_file.read_text().splitlines(),
                [f"{name}={value}" for name, value in FARGATE_SETTINGS.items()],
            )
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(image_file.read_text(), f"EVALVE_BACKEND_IMAGE={image}\n")
            self.assertEqual(deploy(settings).stdout, start)

            changed = {**settings, "EVALVE_LOOP_TASK_SUBNET": "subnet-b"}
            image_inode = image_file.stat().st_ino
            failed = deploy(changed, fail_restart=True)
            self.assertEqual(failed.returncode, 1)
            self.assertTrue(restart_pending.exists())
            self.assertIn("EVALVE_LOOP_TASK_SUBNET=subnet-b\n", settings_file.read_text())
            self.assertEqual(image_file.stat().st_ino, image_inode)
            self.assertEqual(deploy(changed).stdout, restart)
            self.assertFalse(restart_pending.exists())
            self.assertEqual(deploy(changed).stdout, start)

    def test_drain_stops_an_existing_backend_before_kitaru(self):
        backend = power.DRAIN.index(
            "if systemctl cat evalve-backend.service >/dev/null 2>&1; then"
            " systemctl stop evalve-backend.service; fi"
        )
        self.assertLess(
            power.DRAIN.index("systemctl stop evalve-refresh.timer evalve-refresh.service"),
            backend,
        )
        self.assertEqual(
            power.DRAIN[backend + 1 : backend + 3],
            [
                "running=$(docker ps --filter name=^/evalve-backend$ --format '{{.ID}}')",
                'test -z "$running"',
            ],
        )
        self.assertLess(backend, power.DRAIN.index("systemctl stop evalve-kitaru.service"))

    def test_start_starts_the_backend_only_when_its_unit_exists(self):
        self.assertEqual(
            power.START[-1],
            "if systemctl cat evalve-backend.service >/dev/null 2>&1; then"
            " systemctl start evalve-backend.service; fi",
        )

    def test_loop_task_stop_requests_track_tasks_already_draining(self):
        tasks = [
            {"taskArn": "active", "desiredStatus": "RUNNING", "lastStatus": "RUNNING"},
            {"taskArn": "draining", "desiredStatus": "STOPPED", "lastStatus": "STOPPING"},
            {"taskArn": "finished", "desiredStatus": "STOPPED", "lastStatus": "STOPPED"},
        ]
        for action in ("stop", "down"):
            with self.subTest(action=action):
                aws = Mock()
                aws.call.side_effect = [
                    {"taskArns": ["active"]},
                    {"taskArns": ["draining", "finished"]},
                    {"tasks": tasks},
                    {},
                ]
                self.assertEqual(
                    power.request_loop_task_stops(aws, "evalve", action=action),
                    {"active", "draining"},
                )
                self.assertEqual(aws.call.call_count, 4)
                aws.call.assert_called_with(
                    "ecs", "stop-task", "--cluster", "evalve", "--task", "active",
                    "--reason", f"evalve {action}",
                )

    def test_loop_task_stop_requests_skip_ecs_without_a_cluster(self):
        aws = Mock()
        self.assertEqual(power.request_loop_task_stops(aws, None, action="stop"), set())
        aws.call.assert_not_called()

    def test_ecs_wait_reports_only_tasks_left_at_deadline(self):
        aws = Mock()
        aws.call.return_value = {"tasks": [
            {"taskArn": "finished", "lastStatus": "STOPPED"},
            {"taskArn": "unfinished", "lastStatus": "STOPPING"},
        ]}
        with (
            patch.object(power.time, "monotonic", side_effect=[0, 1, 2, 150, 150]),
            redirect_stderr(io.StringIO()) as warnings,
        ):
            count = power.wait_for_loop_tasks(aws, "evalve", {"finished", "unfinished"})
        self.assertEqual(count, 1)
        self.assertIn("150 seconds: unfinished", warnings.getvalue())

    def test_stop_waits_for_ecs_between_server_commands(self):
        for action in ("stop", "down"):
            with self.subTest(action=action):
                aws = Mock()
                events = []
                with (
                    patch.object(power, "instance_state", return_value="running"),
                    patch.object(power, "run_server_command",
                                 side_effect=lambda *args: events.append(args[-1])),
                    patch.object(power, "request_loop_task_stops",
                                 side_effect=lambda *a, **kw: events.append(kw["action"]) or {"task"}),
                    patch.object(power, "wait_for_loop_tasks",
                                 side_effect=lambda *args: events.append(args[-1]) or 1),
                ):
                    count = power.stop(aws, INSTANCE, cluster="evalve", action=action)
                self.assertEqual(count, 1)
                self.assertEqual(events, [power.DRAIN_BACKEND, action, {"task"}, power.DRAIN_KITARU])

    def test_ecs_verification_failure_leaves_kitaru_and_ec2_up(self):
        aws = Mock()
        with (
            patch.object(power, "instance_state", return_value="running"),
            patch.object(power, "run_server_command") as command,
            patch.object(power, "request_loop_task_stops", return_value={"task"}),
            patch.object(power, "wait_for_loop_tasks",
                         side_effect=EvalveError("ECS verification failed")),
            self.assertRaisesRegex(EvalveError, "ECS verification failed"),
        ):
            power.stop(aws, INSTANCE, cluster="evalve")
        command.assert_called_once_with(aws, INSTANCE, power.DRAIN_BACKEND)
        aws.call.assert_not_called()

    def test_stop_never_sends_ec2_stop_after_failed_drain(self):
        aws = Mock()
        with (
            patch.object(power, "instance_state", return_value="running"),
            patch.object(
                power, "run_server_command", side_effect=EvalveError("drain failed")
            ),
        ):
            with self.assertRaisesRegex(EvalveError, "drain failed"):
                power.stop(aws, INSTANCE)
        aws.call.assert_not_called()

    def test_stop_retry_waits_without_sending_stop_again(self):
        aws = Mock()
        with patch.object(power, "instance_state", return_value="stopping"):
            power.stop(aws, INSTANCE)
        aws.call.assert_called_once_with(
            "ec2", "wait", "instance-stopped", "--instance-ids", INSTANCE
        )


    def test_stop_rechecks_ecs_after_ec2_has_stopped(self):
        for state in ("stopped", "stopping", "terminated"):
            with self.subTest(state=state):
                aws = Mock()
                with (
                    patch.object(power, "instance_state", return_value=state),
                    patch.object(power, "run_server_command") as command,
                    patch.object(power, "drain_loop_tasks", return_value=1) as drain,
                ):
                    self.assertEqual(
                        power.stop(aws, INSTANCE, allow_gone=True, cluster="evalve"), 1
                    )
                command.assert_not_called()
                drain.assert_called_once_with(aws, "evalve", action="stop")
                if state == "stopping":
                    aws.call.assert_called_once_with(
                        "ec2", "wait", "instance-stopped", "--instance-ids", INSTANCE
                    )
                else:
                    aws.call.assert_not_called()


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT, region=REGION)
        self.snapshot = {
            "DBSnapshotArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:{SNAPSHOT}",
            "DbiResourceId": DB,
            "DBInstanceIdentifier": "evalve",
            "SnapshotType": "manual",
            "Engine": "postgres",
            "EngineVersion": "16.12",
            "Encrypted": True,
            "Status": "available",
            "KmsKeyId": "key-id",
        }
        self.manifest = {
            "schema_version": 1,
            "account_id": ACCOUNT,
            "region": REGION,
            "snapshot_id": SNAPSHOT,
            "database_resource_id": DB,
            "kms_key_id": "key-id",
            "configuration": dict(CONFIG, database_engine_version="16.12"),
        }

    def test_backup_requires_exact_source_and_secret_version(self):
        self.aws.call.side_effect = [
            {"DBSnapshots": [self.snapshot]},
            {"KeyMetadata": {"KeyState": "Enabled"}},
            {
                "ARN": SECRET,
                "VersionId": VERSION,
                "SecretString": json.dumps(
                    {
                        "jwt_signing_key": "test",
                        "encryption_key": "test",
                        "admin_password": "test",
                    }
                ),
            },
        ]
        backup.verify_manifest(self.aws, self.manifest, SNAPSHOT)
        self.aws.call.assert_called_with(
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            SECRET,
            "--version-id",
            VERSION,
        )

    def test_same_database_name_cannot_substitute_for_resource_id(self):
        self.aws.call.return_value = {
            "DBSnapshots": [dict(self.snapshot, DbiResourceId="db-other-generation")]
        }
        with self.assertRaisesRegex(EvalveError, "does not match"):
            backup.verify_manifest(self.aws, self.manifest, SNAPSHOT)
        self.assertEqual(self.aws.call.call_count, 1)

    def test_retry_reuses_existing_snapshot_without_creating_another(self):
        self.assertEqual(
            backup.take_snapshot(self.aws, SNAPSHOT, DB, self.snapshot), self.snapshot
        )
        self.aws.call.assert_not_called()


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT, region=REGION, profile="evalve")
        self.aws.caller_account.return_value = ACCOUNT
        self.aws.ensure_application_secret.return_value = SECRET
        self.aws.read_document.side_effect = self.read
        self.aws.create_document.side_effect = self.create
        self.aws.delete_document.side_effect = self.delete
        self.documents = {
            STATE_KEY: {
                "outputs": {"deployment": {"value": CONFIG}},
                "resources": [
                    resource(
                        "aws_instance",
                        "server",
                        {"id": INSTANCE, "instance_type": "t3.small"},
                    ),
                    resource(
                        "aws_db_instance",
                        "main",
                        {
                            "identifier": "evalve",
                            "resource_id": DB,
                            "instance_class": "db.t4g.micro",
                        },
                    ),
                ],
            }
        }
        self.events = []

    def read(self, key):
        if key.startswith("evalve/snapshots/"):
            self.events.append("read-manifest")
        return self.documents.get(key), '"etag"' if key in self.documents else None

    def create(self, key, document):
        self.assertNotIn(key, self.documents)
        self.documents[key] = document
        self.events.append(
            "checkpoint" if key == lifecycle.OPERATION_KEY else "save-manifest"
        )

    def delete(self, key, etag):
        self.assertEqual(etag, '"etag"')
        del self.documents[key]
        self.events.append("complete")

    def invoke(self, *, answer="evalve", verification_fails=False, destroy_fails=False):
        def verify(*args):
            self.events.append("verify")
            if verification_fails:
                raise EvalveError("backup verification failed")

        def destroy(*args):
            self.events.append("destroy")
            self.assertEqual(args[-1], DB)
            if destroy_fails:
                raise EvalveError("Terraform interrupted")
            self.documents[STATE_KEY] = {"resources": []}

        def snapshot(*args):
            self.events.append("snapshot")
            return {"EngineVersion": "16.12", "KmsKeyId": "key-id"}

        def find_snapshot(*args):
            return (
                {"Status": "available"}
                if any(key.startswith("evalve/snapshots/") for key in self.documents)
                else None
            )

        with (
            patch("builtins.input", return_value=answer),
            redirect_stdout(io.StringIO()) as output,
            patch.object(
                power, "stop", side_effect=lambda *a, **kw: self.events.append("drain")
            ),
            patch.object(power, "instance_state", return_value="stopped"),
            patch.object(backup, "secret_version", return_value=VERSION),
            patch.object(backup, "get_snapshot", side_effect=find_snapshot),
            patch.object(backup, "take_snapshot", side_effect=snapshot),
            patch.object(backup, "verify_manifest", side_effect=verify),
            patch.object(terraform, "destroy", side_effect=destroy),
        ):
            lifecycle.down(self.aws, Path("/unused"))
        return output.getvalue()

    def test_declining_teardown_leaves_aws_untouched(self):
        self.invoke(answer="no")
        self.assertEqual(self.events, [])
        self.aws.create_document.assert_not_called()

    def test_down_reports_retention_when_agent_repository_exists(self):
        output = self.invoke()
        self.assertIn(
            "The state bucket, application secret, snapshots and agent image repository "
            "will be retained. The agent images continue to cost money for their storage.",
            output,
        )
        self.aws.call.assert_called_once_with(
            "ecr", "describe-repositories", "--repository-names", "evalve-agent-images"
        )

    def test_down_keeps_original_notice_when_agent_repository_is_missing(self):
        self.aws.call.side_effect = AwsError(
            "ecr describe-repositories", "RepositoryNotFoundException"
        )
        output = self.invoke(answer="no")
        self.assertIn(
            "The state bucket, Evalve secrets, and snapshots will be retained.", output
        )
        self.assertNotIn("agent image repository", output)

    def test_down_stops_before_confirmation_if_repository_lookup_fails(self):
        self.aws.call.side_effect = AwsError("ecr describe-repositories", "AccessDenied")
        with self.assertRaisesRegex(AwsError, "AccessDenied"):
            lifecycle.down(self.aws, Path("/unused"))
        self.aws.create_document.assert_not_called()
        self.assertEqual(self.events, [])

    def test_verified_backup_and_metadata_readback_precede_destroy(self):
        output = self.invoke()
        self.assertEqual(
            self.events,
            [
                "checkpoint",
                "drain",
                "read-manifest",
                "snapshot",
                "verify",
                "save-manifest",
                "read-manifest",
                "destroy",
                "complete",
            ],
        )
        self.assertIn("evalve up --restore", output)
        self.assertNotIn(lifecycle.OPERATION_KEY, self.documents)

    def test_verification_failure_cannot_destroy_database(self):
        with self.assertRaisesRegex(EvalveError, "verification failed"):
            self.invoke(verification_fails=True)
        self.assertNotIn("destroy", self.events)
        self.assertIn(lifecycle.OPERATION_KEY, self.documents)

    def test_interrupted_destroy_reuses_snapshot_and_reverifies(self):
        with self.assertRaisesRegex(EvalveError, "Terraform interrupted"):
            self.invoke(destroy_fails=True)
        saved = self.documents[lifecycle.OPERATION_KEY]["snapshot_id"]
        output = self.invoke()
        self.assertIn(saved, output)
        self.assertEqual(self.events.count("snapshot"), 1)
        self.assertEqual(self.events.count("verify"), 2)
        self.assertEqual(self.events.count("drain"), 1)

    def test_down_rechecks_ecs_before_resuming_destroy_with_saved_snapshot(self):
        with self.assertRaisesRegex(EvalveError, "Terraform interrupted"):
            self.invoke(destroy_fails=True)
        self.documents[STATE_KEY]["outputs"]["ecs_cluster_name"] = {"value": "evalve"}

        def drain(aws, cluster, *, action):
            self.assertIs(aws, self.aws)
            self.assertEqual((cluster, action), ("evalve", "down"))
            self.events.append("ecs retry")
            return 1

        self.events.clear()
        with patch.object(power, "drain_loop_tasks", side_effect=drain):
            output = self.invoke()
        self.assertLess(self.events.index("ecs retry"), self.events.index("destroy"))
        self.assertNotIn("drain", self.events)
        self.assertNotIn("snapshot", self.events)
        self.assertIn("Loop tasks stopped: 1", output)

    def test_down_resumes_destroy_when_recorded_ecs_cluster_is_missing(self):
        with self.assertRaisesRegex(EvalveError, "Terraform interrupted"):
            self.invoke(destroy_fails=True)
        self.documents[STATE_KEY]["outputs"]["ecs_cluster_name"] = {"value": "evalve"}
        self.aws.call.reset_mock()
        self.aws.call.side_effect = [
            {},
            AwsError("ecs list-tasks", "ClusterNotFoundException"),
        ]

        self.events.clear()
        output = self.invoke()

        self.assertEqual(self.aws.call.call_count, 2)
        self.aws.call.assert_called_with(
            "ecs", "list-tasks", "--cluster", "evalve", "--desired-status", "RUNNING"
        )
        self.assertIn("Loop tasks stopped: 0", output)
        self.assertIn("destroy", self.events)
        self.assertNotIn("snapshot", self.events)
        self.assertNotIn(lifecycle.OPERATION_KEY, self.documents)

    def test_backend_image_is_saved_for_restore(self):
        image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64
        deployment = self.documents[STATE_KEY]["outputs"]["deployment"]
        deployment["value"] = dict(
            CONFIG, backend_image=image, backend_image_pinned=True
        )
        self.invoke()
        manifests = [
            document
            for key, document in self.documents.items()
            if key.startswith("evalve/snapshots/")
        ]
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["configuration"]["backend_image"], image)
        self.assertTrue(manifests[0]["configuration"]["backend_image_pinned"])

    def test_backend_secret_is_checked_and_saved_for_restore(self):
        backend_secret = SECRET.replace("evalve/application", "evalve/backend")
        self.aws.ensure_backend_secret.return_value = backend_secret
        deployment = self.documents[STATE_KEY]["outputs"]["deployment"]
        deployment["value"] = dict(CONFIG, backend_secret_arn=backend_secret)
        self.invoke()
        self.aws.ensure_backend_secret.assert_called_once_with(
            backend_secret, create=False
        )
        manifests = [
            document
            for key, document in self.documents.items()
            if key.startswith("evalve/snapshots/")
        ]
        self.assertEqual(
            manifests[0]["configuration"]["backend_secret_arn"], backend_secret
        )

    def test_a_missing_backend_secret_stops_down_before_any_change(self):
        backend_secret = SECRET.replace("evalve/application", "evalve/backend")
        self.aws.ensure_backend_secret.side_effect = EvalveError(
            "The deployment's backend secret is missing."
        )
        deployment = self.documents[STATE_KEY]["outputs"]["deployment"]
        deployment["value"] = dict(CONFIG, backend_secret_arn=backend_secret)
        with self.assertRaisesRegex(EvalveError, "backend secret is missing"):
            self.invoke()
        self.assertEqual(self.events, [])
        self.aws.create_document.assert_not_called()

    def test_an_older_checkpoint_with_a_backend_and_no_secret_resumes_to_teardown(self):
        # Settings saved by an older Evalve: a backend image, no Backend secret.
        image = "ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64
        deployment = self.documents[STATE_KEY]["outputs"]["deployment"]
        deployment["value"] = dict(CONFIG, backend_image=image)
        with self.assertRaisesRegex(EvalveError, "Terraform interrupted"):
            self.invoke(destroy_fails=True)
        checkpoint = self.documents[lifecycle.OPERATION_KEY]["configuration"]
        self.assertEqual(checkpoint["backend_image"], image)
        self.assertNotIn("backend_secret_arn", checkpoint)
        self.invoke()
        self.aws.ensure_backend_secret.assert_not_called()
        self.assertEqual(self.events.count("destroy"), 2)
        self.assertNotIn(lifecycle.OPERATION_KEY, self.documents)

    def test_a_deployment_without_a_backend_secret_needs_no_check(self):
        self.invoke()
        self.aws.ensure_backend_secret.assert_not_called()
        manifests = [
            document
            for key, document in self.documents.items()
            if key.startswith("evalve/snapshots/")
        ]
        self.assertNotIn("backend_secret_arn", manifests[0]["configuration"])

    def test_loop_task_ports_are_saved_for_restore(self):
        deployment = self.documents[STATE_KEY]["outputs"]["deployment"]
        deployment["value"] = dict(CONFIG, loop_task_egress_ports=[5432])
        self.invoke()
        manifests = [
            document
            for key, document in self.documents.items()
            if key.startswith("evalve/snapshots/")
        ]
        self.assertEqual(len(manifests), 1)
        self.assertEqual(
            manifests[0]["configuration"]["loop_task_egress_ports"], [5432]
        )


class RestoreTest(unittest.TestCase):
    def test_restore_cli_passes_saved_configuration_and_retains_checkpoint_until_ready(
        self,
    ):
        # A failed Kitaru or backend check must keep the checkpoint for a retry.
        for failing in ("kitaru", "backend", "dashboard", None):
            healthy = failing is None
            with self.subTest(failing=failing):
                aws = Mock(account=ACCOUNT, region=REGION, profile="evalve")
                aws.state_outputs.return_value = {}
                aws.caller_account.return_value = ACCOUNT
                aws.ensure_application_secret.return_value = SECRET
                backend_secret = SECRET.replace("evalve/application", "evalve/backend")
                aws.ensure_backend_secret.return_value = backend_secret
                result = dict(
                    CONFIG,
                    snapshot_id=SNAPSHOT,
                    domain=None,
                    database_engine_version="16.12",
                    backend_image="ghcr.io/evalve-app/evalve-backend@sha256:" + "0" * 64,
                    backend_image_pinned=True,
                    backend_secret_arn=backend_secret,
                    loop_task_egress_ports=[5432],
                )
                with (
                    patch.object(cli, "check_tools"),
                    patch.object(cli, "Aws", return_value=aws),
                    patch("builtins.input", return_value="yes"),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    patch.object(
                        lifecycle, "pending_operation", return_value=(None, None)
                    ),
                    patch.object(lifecycle, "load_restore", return_value=result),
                    patch.object(
                        lifecycle, "record_restore", return_value='"restore-etag"'
                    ),
                    patch.object(
                        terraform,
                        "operation_directory",
                        return_value=nullcontext(Path("/unused")),
                    ),
                    patch.object(
                        terraform,
                        "apply",
                        return_value={
                            "url": "https://example.sslip.io",
                            "dashboard_url": "https://dashboard.example.sslip.io",
                            "instance_id": INSTANCE,
                            **FARGATE_OUTPUTS,
                        },
                        # A restore needs the repository of the agent images
                        # before Terraform runs.
                        side_effect=lambda *a: (
                            aws.ensure_agent_image_repository.assert_called_once_with(),
                            DEFAULT,
                        )[1],
                    ) as apply,
                    patch.object(
                        cli,
                        "wait_for_kitaru",
                        side_effect=EvalveError("not ready") if failing == "kitaru" else None,
                    ),
                    patch.object(power, "deploy_backend") as deploy,
                    patch.object(
                        cli,
                        "wait_for_backend",
                        side_effect=EvalveError("not ready") if failing == "backend" else None,
                    ),
                    patch.object(power, "deploy_dashboard") as deploy_dashboard,
                    patch.object(
                        cli,
                        "wait_for_dashboard",
                        side_effect=EvalveError("not ready") if failing == "dashboard" else None,
                    ),
                ):
                    code = cli.main(
                        ["up", "--restore", SNAPSHOT, "--monthly-estimate", "35.00"]
                    )
                variables = apply.call_args.args[2]
                self.assertEqual(variables["snapshot_id"], SNAPSHOT)
                self.assertEqual(variables["application_secret_version"], VERSION)
                self.assertEqual(variables["backup_retention_days"], 1)
                self.assertIsNone(variables["domain"])
                self.assertEqual(variables["backend_image"], result["backend_image"])
                self.assertTrue(variables["backend_image_pinned"])
                self.assertEqual(variables["loop_task_egress_ports"], [5432])
                # The restore reuses the saved secret, so the password stays the same.
                aws.ensure_backend_secret.assert_called_once_with(backend_secret)
                self.assertEqual(variables["backend_secret_arn"], backend_secret)
                if failing != "kitaru":
                    # The restored server gets the saved image from up.
                    deploy.assert_called_once_with(
                        aws, INSTANCE, result["backend_image"], FARGATE_SETTINGS
                    )
                if failing != "kitaru":
                    # The restored server gets the dashboard from up as well,
                    # even when the backend fails (ADR 0005 point 6).
                    deploy_dashboard.assert_called_once_with(
                        aws, INSTANCE, cli.DEFAULT_DASHBOARD_URL, cli.DEFAULT_DASHBOARD_SHA256
                    )
                self.assertEqual(code, 0 if healthy else 1)
                if healthy:
                    aws.delete_document.assert_called_once_with(
                        lifecycle.OPERATION_KEY, '"restore-etag"'
                    )
                else:
                    aws.delete_document.assert_not_called()

    def test_restore_refuses_existing_resources(self):
        aws = Mock()
        aws.read_document.return_value = (
            {"resources": [resource("aws_instance", "server", {"id": INSTANCE})]},
            None,
        )
        with self.assertRaisesRegex(EvalveError, "Run down"):
            lifecycle.load_restore(aws, SNAPSHOT, None)

    def test_restore_uses_saved_secret_version_and_starts_with_sslip(self):
        aws = Mock()
        manifest = {"configuration": CONFIG}
        aws.read_document.side_effect = [
            ({"resources": []}, None),
            (manifest, '"etag"'),
        ]
        with patch.object(backup, "verify_manifest") as verify:
            result = lifecycle.load_restore(aws, SNAPSHOT, None)
        verify.assert_called_once_with(aws, manifest, SNAPSHOT)
        self.assertEqual(result, dict(CONFIG, snapshot_id=SNAPSHOT, domain=None))


class DeletionPlanTest(unittest.TestCase):
    def test_destroy_rejects_an_unverified_database_even_with_deletion_protection_off(
        self,
    ):
        wrong_database = {
            "resource_id": "db-other-generation",
            "deletion_protection": False,
            "skip_final_snapshot": True,
        }
        destroy_plan = {
            "resource_changes": [
                {
                    "mode": "managed",
                    "type": "aws_db_instance",
                    "change": {"actions": ["delete"], "before": wrong_database},
                }
            ]
        }
        with (
            patch.object(
                terraform,
                "terraform_workspace",
                return_value=nullcontext((Path("/unused"), {})),
            ),
            patch.object(terraform, "saved_plan", side_effect=[{}, destroy_plan]),
            patch.object(terraform, "unlocks_only", return_value=False),
            patch.object(terraform, "apply_plan") as apply,
        ):
            with self.assertRaisesRegex(
                EvalveError, "does not match the verified backup"
            ):
                terraform.destroy(Mock(), Path("/unused"), {}, DB)
        apply.assert_not_called()

    def test_only_deletion_guard_changes_are_permitted(self):
        before = {
            "resource_id": DB,
            "deletion_protection": True,
            "skip_final_snapshot": False,
            "engine_version": "16",
        }
        change = {
            "actions": ["update"],
            "before": before,
            "after": dict(before, deletion_protection=False, skip_final_snapshot=True),
        }
        plan = {
            "configuration": {
                "root_module": {
                    "resources": [
                        {
                            "address": "aws_db_instance.main",
                            "expressions": {key: {} for key in before},
                        }
                    ]
                }
            },
            "resource_changes": [
                {"mode": "managed", "address": "aws_db_instance.main", "change": change}
            ],
        }
        self.assertTrue(terraform.unlocks_only(plan, DB))
        change["after"]["engine_version"] = "17"
        with self.assertRaisesRegex(
            EvalveError, "more than the database deletion settings"
        ):
            terraform.unlocks_only(plan, DB)


class DashboardInstallTest(unittest.TestCase):
    """Run the install script in bash against a temporary folder and file:// URLs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "dashboard"
        self.root.mkdir()

    def bundle(self, name, title="evalve"):
        site = self.base / f"{name}-site"
        (site / "assets").mkdir(parents=True)
        (site / "index.html").write_text(f"<title>{title}</title>")
        (site / "assets" / "app.js").write_text(name)
        archive = self.base / f"{name}.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(site, arcname=".")
        return archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest()

    def install(self, url, sha):
        script = power.dashboard_install_script(url, sha, str(self.root))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def current(self):
        return os.readlink(self.root / "current")

    def releases(self):
        return sorted(path.name for path in (self.root / "releases").iterdir())

    def test_first_install_links_current_relatively_and_leaves_no_temp_files(self):
        url, sha = self.bundle("one")
        self.assertEqual(self.install(url, sha).returncode, 0)
        self.assertEqual(self.current(), f"releases/{sha}")
        self.assertTrue((self.root / "current" / "index.html").is_file())
        self.assertEqual(
            [path.name for path in self.root.iterdir() if path.name.startswith(".")], []
        )

    def test_an_installed_release_is_not_downloaded_again(self):
        url, sha = self.bundle("one")
        self.install(url, sha)
        missing = (self.base / "missing.tgz").as_uri()
        self.assertEqual(self.install(missing, sha).returncode, 0)
        self.assertEqual(self.current(), f"releases/{sha}")

    def test_a_wrong_checksum_fails_and_keeps_the_served_release(self):
        url, sha = self.bundle("one")
        self.install(url, sha)
        other_url, _ = self.bundle("two")
        result = self.install(other_url, "b" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), f"releases/{sha}")
        self.assertEqual(self.releases(), [sha])

    def test_a_failed_download_fails_and_keeps_the_served_release(self):
        url, sha = self.bundle("one")
        self.install(url, sha)
        result = self.install((self.base / "missing.tgz").as_uri(), "c" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.current(), f"releases/{sha}")

    def test_a_bundle_without_index_html_is_refused(self):
        site = self.base / "empty-site"
        site.mkdir()
        (site / "other.html").write_text("x")
        archive = self.base / "empty.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(site, arcname=".")
        sha = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertNotEqual(self.install(archive.as_uri(), sha).returncode, 0)
        self.assertFalse((self.root / "current").exists())

    def test_an_upgrade_keeps_the_previous_release_and_removes_older_ones(self):
        bundles = [self.bundle(name) for name in ("one", "two", "three")]
        for url, sha in bundles:
            self.assertEqual(self.install(url, sha).returncode, 0)
        (_, first), (_, second), (_, third) = bundles
        self.assertEqual(self.current(), f"releases/{third}")
        self.assertEqual(self.releases(), sorted([second, third]))
        # The kept previous release switches back without a download.
        missing = (self.base / "missing.tgz").as_uri()
        self.assertEqual(self.install(missing, second).returncode, 0)
        self.assertEqual(self.current(), f"releases/{second}")
        self.assertNotIn(first, self.releases())

    def test_repeating_an_install_after_an_upgrade_keeps_the_previous_release(self):
        (old_url, old), (new_url, new) = self.bundle("one"), self.bundle("two")
        self.install(old_url, old)
        self.install(new_url, new)
        # A plain up repeats the install of the release that is already served.
        self.assertEqual(self.install(new_url, new).returncode, 0)
        self.assertEqual(self.current(), f"releases/{new}")
        self.assertEqual(self.releases(), sorted([old, new]))
