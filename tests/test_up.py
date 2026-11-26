import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from evalve import EvalveError, cli, terraform
from evalve.aws import PUBLIC_ACCESS_BLOCK, Aws, AwsError

ACCOUNT = "123456789012"
SECRET = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:evalve/application-test"


class CredentialsTest(unittest.TestCase):
    def test_prints_only_the_dashboard_login_using_the_deployed_secret_version(self):
        for version in (None, "saved-version"):
            with self.subTest(version=version):
                aws = Mock(account=ACCOUNT, region="us-east-1")
                aws.state_outputs.return_value = {
                    "deployment": {
                        "application_secret_arn": SECRET,
                        "application_secret_version": version,
                    }
                }
                aws.call.return_value = {
                    "ARN": SECRET,
                    "VersionId": version or "current-version",
                    "SecretString": json.dumps(
                        {
                            "admin_password": "test-password",
                            "jwt_signing_key": "test-signing-key",
                            "encryption_key": "test-encryption-key",
                        }
                    ),
                }
                with (
                    patch.object(cli, "check_tools") as check,
                    patch.object(cli, "Aws", return_value=aws),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(cli.main(["credentials"]), 0)
                check.assert_called_once_with(needs_terraform=False)
                arguments = ("--version-id", version) if version else ()
                aws.call.assert_called_once_with(
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    SECRET,
                    *arguments,
                )
                self.assertEqual(
                    output.getvalue(), "Username: default\nPassword: test-password\n"
                )

    def test_help_names_the_admin_account_and_no_kitaru_dashboard(self):
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("Print the bootstrap Admin account login", output.getvalue())
        self.assertNotIn("Kitaru dashboard", output.getvalue())

    def test_missing_deployment_refuses_to_fetch_any_secret(self):
        aws = Mock()
        aws.state_outputs.return_value = {}
        with (
            patch.object(cli, "check_tools"),
            patch.object(cli, "Aws", return_value=aws),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["credentials"]), 1)
        self.assertIn("Run up first", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        aws.call.assert_not_called()


class UpTest(unittest.TestCase):
    def invoke(
        self,
        args,
        answers,
        *,
        existing=None,
        caller_account=ACCOUNT,
        dashboard_error=None,
        immutable_repository=True,
    ):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            profile="evalve",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
        )
        aws.state_outputs.return_value = existing or {}
        aws.read_document.return_value = (None, None)
        aws.caller_account.return_value = caller_account
        aws.ensure_application_secret.return_value = SECRET
        aws.ensure_agent_image_repository.return_value = immutable_repository
        self.repository_checked_before_apply = None

        def applied(*a):
            self.repository_checked_before_apply = (
                aws.ensure_agent_image_repository.called
            )
            return {
                "url": "https://evalve.example.com",
                "dashboard_url": "https://dashboard.evalve.example.com",
                "instance_id": "i-0123",
            }

        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.object(cli, "check_tools"),
            patch.object(cli, "Aws", return_value=aws) as factory,
            patch.object(
                terraform,
                "operation_directory",
                return_value=nullcontext(Path("/unused")),
            ),
            patch.object(
                terraform,
                "apply",
                side_effect=applied,
            ) as apply,
            patch.object(cli, "verify_dns") as dns,
            patch.object(cli, "wait_for_kitaru") as ready,
            patch.object(cli.power, "deploy_backend") as self.deploy,
            patch.object(cli, "wait_for_backend") as self.backend_check,
            patch.object(cli.power, "deploy_dashboard") as self.deploy_dashboard,
            patch.object(cli, "wait_for_dashboard") as self.dashboard_check,
            patch("builtins.input", side_effect=answers),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            # Order: Kitaru is ready, then the backend image is deployed and
            # checked, then the dashboard is installed and checked.
            ready.side_effect = lambda *a: self.deploy.assert_not_called()
            self.deploy.side_effect = lambda *a: self.backend_check.assert_not_called()
            self.backend_check.side_effect = (
                lambda *a: self.deploy_dashboard.assert_not_called()
            )
            self.deploy_dashboard.side_effect = (
                lambda *a: self.dashboard_check.assert_not_called()
            )
            def dashboard_answers(*a):
                aws.delete_document.assert_not_called()
                if dashboard_error:
                    raise dashboard_error

            self.dashboard_check.side_effect = dashboard_answers
            code = cli.main(["up", *args])
        factory.assert_called_once_with("evalve", None)
        return code, aws, apply, ready, dns, output.getvalue(), errors.getvalue()

    def test_declining_cost_review_only_reads_aws(self):
        code, aws, apply, ready, _, output, _ = self.invoke(
            ["--server-type", "t3.small", "--database-class", "db.t4g.micro"],
            ["32.50", "n"],
        )
        self.assertEqual(code, 0)
        self.assertIn("$32.50/month", output)
        self.assertIn(ACCOUNT, output)
        aws.ensure_bucket.assert_not_called()
        aws.ensure_application_secret.assert_not_called()
        aws.ensure_agent_image_repository.assert_not_called()
        apply.assert_not_called()
        ready.assert_not_called()

    def test_confirmed_first_up_bootstraps_then_applies_and_checks_readiness(self):
        code, aws, apply, ready, _, output, _ = self.invoke(
            [
                "--server-type",
                "c7i-flex.large",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "40",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [call[0] for call in aws.method_calls],
            [
                "read_document",
                "state_outputs",
                "verify_server_type",
                "caller_account",
                "read_document",
                "ensure_bucket",
                "ensure_application_secret",
                "ensure_agent_image_repository",
            ],
        )
        self.assertTrue(self.repository_checked_before_apply)
        self.assertEqual(apply.call_args.args[2]["application_secret_arn"], SECRET)
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 7)
        self.assertIsNone(apply.call_args.args[2]["domain"])
        ready.assert_called_once_with("https://evalve.example.com")
        self.assertIn("Bundled dashboard: https://evalve.example.com\n", output)
        self.assertIn(
            "evalve credentials with the same AWS profile and region for the bootstrap Admin account login.",
            output,
        )

    def test_up_installs_and_checks_the_pinned_dashboard(self):
        code, _, _, _, _, output, _ = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "35",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.deploy_dashboard.assert_called_once()
        self.assertEqual(
            self.deploy_dashboard.call_args.args[1:],
            ("i-0123", cli.DEFAULT_DASHBOARD_URL, cli.DEFAULT_DASHBOARD_SHA256),
        )
        self.dashboard_check.assert_called_once_with(
            "https://dashboard.evalve.example.com"
        )
        # The confirmation shows the bundle, and the end shows its address.
        self.assertLess(
            output.index(f"Evalve dashboard: {cli.DEFAULT_DASHBOARD_URL}"),
            output.index("Apply Evalve") if "Apply Evalve" in output else len(output),
        )
        self.assertIn(
            "Evalve dashboard: https://dashboard.evalve.example.com\n", output
        )

    def test_a_failed_dashboard_check_fails_up(self):
        code, _, _, _, _, output, errors = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "35",
            ],
            ["yes"],
            dashboard_error=EvalveError("not answering"),
        )
        self.assertEqual(code, 1)
        self.assertIn("not answering", errors)
        self.assertNotIn("Bundled dashboard:", output)

    def test_the_dashboard_pin_is_a_release_asset_and_a_sha256(self):
        self.assertRegex(
            cli.DEFAULT_DASHBOARD_URL,
            r"^https://github\.com/evalve-app/evalve-ui/releases/download/"
            r"v\d+\.\d+\.\d+/dashboard\.tgz$",
        )
        self.assertRegex(cli.DEFAULT_DASHBOARD_SHA256, r"^[a-f0-9]{64}$")

    def test_first_up_can_use_one_day_of_automated_backups(self):
        code, _, apply, _, _, output, _ = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--backup-retention-days",
                "1",
                "--monthly-estimate",
                "36",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 1)
        self.assertIn("backup retention: 1 day", output)

    def test_profile_account_change_blocks_writes(self):
        code, aws, apply, _, _, _, errors = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "35",
            ],
            ["y"],
            caller_account="999999999999",
        )
        self.assertEqual(code, 1)
        self.assertIn("changed accounts", errors)
        aws.ensure_bucket.assert_not_called()
        apply.assert_not_called()

    def test_custom_domain_reuses_existing_sizes_and_application_secret(self):
        existing = {
            "public_ip": "203.0.113.10",
            "deployment": {
                "server_instance_type": "t3.small",
                "database_instance_class": "db.t4g.micro",
                "backup_retention_days": 1,
                "application_secret_arn": SECRET,
                "domain": None,
            },
        }
        code, aws, apply, _, dns, _, _ = self.invoke(
            ["--domain", "evalve.example.com", "--monthly-estimate", "35"],
            ["yes"],
            existing=existing,
        )
        self.assertEqual(code, 0)
        # Both names are checked before any AWS change.
        self.assertEqual(
            [call.args for call in dns.call_args_list],
            [
                ("evalve.example.com", "203.0.113.10"),
                ("dashboard.evalve.example.com", "203.0.113.10"),
            ],
        )
        aws.ensure_application_secret.assert_called_once_with(SECRET)
        self.assertEqual(apply.call_args.args[2]["domain"], "evalve.example.com")
        self.assertEqual(apply.call_args.args[2]["server_instance_type"], "t3.small")
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 1)

    BACKEND = "ghcr.io/evalve-app/evalve-backend@sha256:" + "a" * 64
    NEW_BACKEND = "ghcr.io/evalve-app/evalve-backend@sha256:" + "b" * 64
    REPLACES = "replaces the EC2 server"
    IN_PLACE = "restarts only the backend container"

    def invoke_existing(self, args, saved_backend, pinned=True):
        deployment = {
            "server_instance_type": "t3.small",
            "database_instance_class": "db.t4g.micro",
            "application_secret_arn": SECRET,
            "domain": None,
        }
        if saved_backend is not None:
            deployment["backend_image"] = saved_backend
            deployment["backend_image_pinned"] = pinned
        with patch.object(cli.power, "instance_state", return_value="running"):
            return self.invoke(
                [*args, "--monthly-estimate", "35"],
                ["yes"],
                existing={"instance_id": "i-0123", "deployment": deployment},
            )

    def test_up_checks_the_backend_after_kitaru_only_when_there_is_one(self):
        code, _, _, ready, _, output, _ = self.invoke_existing([], self.BACKEND)
        self.assertEqual(code, 0)
        ready.assert_called_once_with("https://evalve.example.com")
        self.deploy.assert_called_once()
        self.assertEqual(self.deploy.call_args.args[1:], ("i-0123", self.BACKEND))
        self.backend_check.assert_called_once_with(
            "https://evalve.example.com", self.BACKEND
        )
        self.assertIn("Evalve backend: https://evalve.example.com/evalve/health", output)

        code, _, _, _, _, output, _ = self.invoke_existing([], None)
        self.assertEqual(code, 0)
        self.deploy.assert_not_called()
        self.backend_check.assert_not_called()
        self.assertNotIn("/evalve/health", output)

    def test_plain_up_keeps_a_pinned_backend_without_a_warning(self):
        code, _, apply, _, _, output, _ = self.invoke_existing([], self.BACKEND)
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[2]["backend_image"], self.BACKEND)
        self.assertTrue(apply.call_args.args[2]["backend_image_pinned"])
        self.assertIn(f"Evalve backend: {self.BACKEND} (pinned)\n", output)
        self.assertNotIn(self.REPLACES, output)

    def test_plain_up_moves_an_unpinned_backend_to_the_default_in_place(self):
        code, _, apply, _, _, output, _ = self.invoke_existing(
            [], self.BACKEND, pinned=False
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            apply.call_args.args[2]["backend_image"], cli.DEFAULT_BACKEND_IMAGE
        )
        self.assertFalse(apply.call_args.args[2]["backend_image_pinned"])
        self.assertIn(f"Evalve backend: {cli.DEFAULT_BACKEND_IMAGE}\n", output)
        self.assertIn(self.IN_PLACE, output)
        self.assertNotIn(self.REPLACES, output)

    def test_choose_backend_follows_the_default_unless_pinned(self):
        D, B, N = cli.DEFAULT_BACKEND_IMAGE, self.BACKEND, self.NEW_BACKEND
        for flag, existing, image, pinned in (
            (None, {}, D, False),  # first up
            (None, {"backend_image": B}, D, False),  # saved before the marker existed
            (None, {"backend_image": B, "backend_image_pinned": False}, D, False),
            (None, {"backend_image": ""}, "", False),  # off stays off
            (None, {"backend_image": B, "backend_image_pinned": True}, B, True),
            (None, {"backend_image": "", "backend_image_pinned": True}, "", True),
            (N, {"backend_image": B}, N, True),  # a digest pins
            ("", {"backend_image": B}, "", True),  # removal pins
            ("default", {"backend_image": B, "backend_image_pinned": True}, D, False),
            ("default", {}, D, False),
        ):
            with self.subTest(flag=flag, existing=existing):
                self.assertEqual(cli.choose_backend(flag, existing), (image, pinned))

    def test_first_up_without_a_flag_deploys_the_default_backend(self):
        code, _, apply, _, _, output, _ = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "35",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            apply.call_args.args[2]["backend_image"], cli.DEFAULT_BACKEND_IMAGE
        )
        self.assertFalse(apply.call_args.args[2]["backend_image_pinned"])
        self.assertEqual(
            self.deploy.call_args.args[1:], ("i-0123", cli.DEFAULT_BACKEND_IMAGE)
        )

    def test_default_flag_value_is_accepted(self):
        self.assertEqual(cli.backend_image("default"), "default")

    def test_turning_the_backend_on_or_off_warns_about_replacement(self):
        for flag, saved in ((self.BACKEND, None), ("", self.BACKEND)):
            with self.subTest(flag=flag):
                code, _, apply, _, _, output, _ = self.invoke_existing(
                    ["--backend-image", flag], saved
                )
                self.assertEqual(code, 0)
                self.assertEqual(apply.call_args.args[2]["backend_image"], flag)
                self.assertIn(self.REPLACES, output)
                # up reinstalls the dashboard, so there is no manual step left.
                self.assertNotIn("deploy-dashboard.sh", output)
                self.assertNotIn(self.IN_PLACE, output)

    def test_a_new_backend_image_restarts_only_the_backend(self):
        code, _, apply, _, _, output, _ = self.invoke_existing(
            ["--backend-image", self.NEW_BACKEND], self.BACKEND
        )
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[2]["backend_image"], self.NEW_BACKEND)
        self.assertIn(self.IN_PLACE, output)
        self.assertNotIn(self.REPLACES, output)
        self.assertEqual(self.deploy.call_args.args[1:], ("i-0123", self.NEW_BACKEND))

    def test_same_backend_or_none_saved_gives_no_warning(self):
        for args, saved, shown in (
            (["--backend-image", self.BACKEND], self.BACKEND, f"{self.BACKEND} (pinned)"),
            ([], None, "none"),
        ):
            with self.subTest(saved=saved):
                code, _, apply, _, _, output, _ = self.invoke_existing(args, saved)
                self.assertEqual(code, 0)
                self.assertIn(f"Evalve backend: {shown}\n", output)
                self.assertNotIn(self.REPLACES, output)
                self.assertNotIn(self.IN_PLACE, output)

    def test_first_up_with_a_backend_has_no_server_to_replace(self):
        code, _, apply, _, _, output, _ = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--backend-image",
                self.BACKEND,
                "--monthly-estimate",
                "35",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[2]["backend_image"], self.BACKEND)
        self.assertNotIn(self.REPLACES, output)
        # A new server gets its image from up, not from user_data.
        self.assertEqual(self.deploy.call_args.args[1:], ("i-0123", self.BACKEND))

    def test_backend_image_tag_is_refused_before_aws(self):
        with (
            patch.object(cli, "Aws") as factory,
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit),
        ):
            cli.main(["up", "--backend-image", "ghcr.io/evalve-app/evalve-backend:v0.1.0"])
        self.assertIn("backend image digest", errors.getvalue())
        factory.assert_not_called()

    def test_custom_domain_requires_an_existing_ip(self):
        code, aws, apply, _, dns, _, errors = self.invoke(
            [
                "--domain",
                "evalve.example.com",
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
            ],
            [],
        )
        self.assertEqual(code, 1)
        self.assertIn("default sslip.io address", errors)
        aws.ensure_bucket.assert_not_called()
        dns.assert_not_called()
        apply.assert_not_called()

    MUTABLE_TAGS = "has mutable tags"

    def test_up_with_a_backend_checks_the_agent_image_repository_before_terraform(
        self,
    ):
        code, aws, _, _, _, output, _ = self.invoke_existing([], self.BACKEND)
        self.assertEqual(code, 0)
        aws.ensure_agent_image_repository.assert_called_once_with()
        self.assertTrue(self.repository_checked_before_apply)
        self.assertNotIn(self.MUTABLE_TAGS, output)

    def test_up_without_a_backend_leaves_the_agent_image_repository_alone(self):
        for args, saved in (([], ""), (["--backend-image", ""], self.BACKEND)):
            with self.subTest(args=args, saved=saved):
                code, aws, apply, _, _, _, _ = self.invoke_existing(args, saved)
                self.assertEqual(code, 0)
                apply.assert_called_once()
                self.assertEqual(aws.ensure_agent_image_repository.mock_calls, [])

    def test_up_warns_about_an_existing_repository_with_mutable_tags(self):
        with patch.object(cli.power, "instance_state", return_value="running"):
            code, _, apply, _, _, output, _ = self.invoke(
                ["--monthly-estimate", "35"],
                ["yes"],
                existing=self.with_ports(None),
                immutable_repository=False,
            )
        self.assertEqual(code, 0)
        self.assertIn(self.MUTABLE_TAGS, output)
        apply.assert_called_once()

    def with_ports(self, ports):
        deployment = {
            "server_instance_type": "t3.small",
            "database_instance_class": "db.t4g.micro",
            "application_secret_arn": SECRET,
            "domain": None,
            "backend_image": self.BACKEND,
            "backend_image_pinned": True,
        }
        if ports is not None:
            deployment["loop_task_egress_ports"] = ports
        return {"instance_id": "i-0123", "deployment": deployment}

    def applied_ports(self, args, saved):
        with patch.object(cli.power, "instance_state", return_value="running"):
            code, _, apply, _, _, _, errors = self.invoke(
                [*args, "--monthly-estimate", "35"],
                ["yes"],
                existing=self.with_ports(saved),
            )
        self.assertEqual((code, errors), (0, ""))
        return apply.call_args.args[2].get("loop_task_egress_ports", [])

    def test_loop_task_port_sets_the_list_and_can_repeat(self):
        self.assertEqual(self.applied_ports(["--loop-task-port", "5432"], None), [5432])
        self.assertEqual(
            self.applied_ports(
                ["--loop-task-port", "5432", "--loop-task-port", "6379"], [8080]
            ),
            [5432, 6379],
        )

    def test_no_loop_task_ports_empties_the_list(self):
        self.assertEqual(self.applied_ports(["--no-loop-task-ports"], [5432]), [])

    def test_up_with_neither_port_flag_keeps_the_saved_list(self):
        self.assertEqual(self.applied_ports([], [5432]), [5432])
        self.assertEqual(self.applied_ports([], None), [])

    def test_loop_task_port_refuses_443_and_values_that_are_not_ports(self):
        for value in ("443", "0", "70000", "5432.5", "postgres"):
            with self.subTest(value), self.assertRaises(SystemExit):
                self.invoke(["--loop-task-port", value], [])

    def test_the_two_port_flags_cannot_be_used_together(self):
        with self.assertRaises(SystemExit):
            self.invoke(["--loop-task-port", "5432", "--no-loop-task-ports"], [])


class AwsBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.aws = Aws.__new__(Aws)
        self.aws.account = ACCOUNT
        self.aws.region = "us-west-2"
        self.aws.bucket = f"evalve-state-{ACCOUNT}-us-west-2"

    def test_new_bucket_is_private_and_versioned(self):
        with (
            patch.object(self.aws, "bucket_exists", return_value=False),
            patch.object(self.aws, "call") as create,
            patch.object(
                self.aws,
                "bucket_call",
                side_effect=[{"LocationConstraint": "us-west-2"}, {}, {}, {}, {}],
            ) as bucket,
        ):
            self.aws.ensure_bucket()
        create.assert_called_once_with(
            "s3api",
            "create-bucket",
            "--bucket",
            self.aws.bucket,
            "--create-bucket-configuration",
            "LocationConstraint=us-west-2",
        )
        self.assertEqual(
            [call.args[0] for call in bucket.call_args_list],
            [
                "get-bucket-location",
                "get-public-access-block",
                "put-public-access-block",
                "get-bucket-versioning",
                "put-bucket-versioning",
            ],
        )
        self.assertEqual(
            json.loads(bucket.call_args_list[2].args[-1]), PUBLIC_ACCESS_BLOCK
        )

    def test_existing_private_versioned_bucket_needs_no_writes(self):
        with (
            patch.object(self.aws, "bucket_exists", return_value=True),
            patch.object(self.aws, "call") as create,
            patch.object(
                self.aws,
                "bucket_call",
                side_effect=[
                    {"LocationConstraint": "us-west-2"},
                    {"PublicAccessBlockConfiguration": PUBLIC_ACCESS_BLOCK},
                    {"Status": "Enabled"},
                ],
            ) as bucket,
        ):
            self.aws.ensure_bucket()
        create.assert_not_called()
        self.assertTrue(
            all(call.args[0].startswith("get-") for call in bucket.call_args_list)
        )

    def test_unverified_bucket_ownership_fails_closed(self):
        with patch.object(
            self.aws, "bucket_call", side_effect=AwsError("head-bucket", "403")
        ):
            with self.assertRaisesRegex(EvalveError, "Cannot verify access"):
                self.aws.bucket_exists()

    def test_state_preview_reads_remote_outputs_without_mutation(self):
        def download(action, _, key, filename):
            self.assertEqual(action, "get-object")
            self.assertEqual(key, "evalve/terraform.tfstate")
            Path(filename).write_text(
                '{"outputs":{"public_ip":{"value":"203.0.113.10"}}}'
            )
            return {}

        with (
            patch.object(self.aws, "bucket_exists", return_value=True),
            patch.object(self.aws, "bucket_call", side_effect=download),
        ):
            self.assertEqual(self.aws.state_outputs(), {"public_ip": "203.0.113.10"})

    def test_secret_creation_uses_stdin_and_retry_reuses_arn(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=[
                AwsError("describe-secret", "ResourceNotFoundException"),
                {"ARN": SECRET},
                {"ARN": SECRET, "Tags": [{"Key": "ManagedBy", "Value": "evalve"}]},
            ],
        ) as call:
            self.assertEqual(self.aws.ensure_application_secret(), SECRET)
            self.assertEqual(self.aws.ensure_application_secret(), SECRET)
        create = call.call_args_list[1]
        self.assertIn("file:///dev/stdin", create.args)
        payload = json.loads(create.kwargs["payload"])
        self.assertEqual(
            set(payload), {"jwt_signing_key", "encryption_key", "admin_password"}
        )
        self.assertTrue(all(value not in create.args for value in payload.values()))
        self.assertEqual(call.call_args_list[-1].args[1], "describe-secret")

    def test_missing_existing_secret_cannot_be_replaced(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=AwsError("describe-secret", "ResourceNotFoundException"),
        ) as call:
            with self.assertRaisesRegex(EvalveError, "Recover the original secret"):
                self.aws.ensure_application_secret(SECRET)
        call.assert_called_once_with(
            "secretsmanager", "describe-secret", "--secret-id", SECRET
        )

    DESCRIBE_REPOSITORY = (
        "ecr",
        "describe-repositories",
        "--repository-names",
        "evalve-agent-images",
    )

    def test_missing_agent_image_repository_is_made_with_immutable_tags(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=[
                AwsError("ecr describe-repositories", "RepositoryNotFoundException"),
                {"repository": {"imageTagMutability": "IMMUTABLE"}},
            ],
        ) as call:
            self.assertTrue(self.aws.ensure_agent_image_repository())
        self.assertEqual(
            [item.args for item in call.call_args_list],
            [
                self.DESCRIBE_REPOSITORY,
                (
                    "ecr",
                    "create-repository",
                    "--repository-name",
                    "evalve-agent-images",
                    "--image-tag-mutability",
                    "IMMUTABLE",
                ),
            ],
        )

    def test_existing_agent_image_repository_is_only_read(self):
        for mutability in ("IMMUTABLE", "MUTABLE"):
            with (
                self.subTest(mutability),
                patch.object(
                    self.aws,
                    "call",
                    return_value={
                        "repositories": [{"imageTagMutability": mutability}]
                    },
                ) as call,
            ):
                self.assertEqual(
                    self.aws.ensure_agent_image_repository(),
                    mutability == "IMMUTABLE",
                )
                call.assert_called_once_with(*self.DESCRIBE_REPOSITORY)

    def test_agent_image_repository_made_by_a_concurrent_up_is_used(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=[
                AwsError("ecr describe-repositories", "RepositoryNotFoundException"),
                AwsError("ecr create-repository", "RepositoryAlreadyExistsException"),
            ],
        ):
            self.assertTrue(self.aws.ensure_agent_image_repository())

    def test_agent_image_repository_that_cannot_be_read_stops_up(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=AwsError("ecr describe-repositories", "AccessDeniedException"),
        ) as call:
            with self.assertRaises(AwsError):
                self.aws.ensure_agent_image_repository()
        call.assert_called_once()


class TerraformApplyTest(unittest.TestCase):
    def test_remote_state_write_failure_preserves_state_and_blocks_retry(self):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
            profile="evalve",
            env={},
        )

        def run(workspace, env, *arguments, capture=False):
            if arguments[0] == "apply":
                (workspace / "errored.tfstate").write_text("recoverable state")
                raise EvalveError("Terraform apply failed")
            return "{}"

        with (
            tempfile.TemporaryDirectory() as home,
            patch.object(Path, "home", return_value=Path(home)),
            patch.object(terraform, "run_terraform", side_effect=run),
        ):
            with terraform.operation_directory(aws) as directory:
                with self.assertRaisesRegex(EvalveError, "Recovery state was saved"):
                    terraform.apply(aws, directory, {"account_id": ACCOUNT})
            self.assertEqual(
                (directory / "errored.tfstate").read_text(), "recoverable state"
            )
            with self.assertRaisesRegex(EvalveError, "Reconcile it with S3"):
                with terraform.operation_directory(aws):
                    self.fail("A recovery file must block retries")

    def test_saved_plan_requires_remote_lock_and_rejects_database_replacement(self):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
            profile="evalve",
            env={},
        )
        commands = []

        def run(workspace, env, *arguments, capture=False):
            commands.append(arguments[0])
            if arguments[0] == "init":
                backend = (workspace / "backend.tf").read_text()
                self.assertIn('backend "s3"', backend)
                self.assertIn("use_lockfile        = true", backend)
                self.assertIn(f'allowed_account_ids = ["{ACCOUNT}"]', backend)
                self.assertIn(
                    "export-credentials", Path(env["AWS_CONFIG_FILE"]).read_text()
                )
                self.assertTrue((workspace / ".terraform.lock.hcl").is_file())
            if arguments[0] == "show":
                return json.dumps(
                    {
                        "resource_changes": [
                            {
                                "address": "aws_db_instance.main",
                                "change": {"actions": ["delete", "create"]},
                            }
                        ]
                    }
                )
            return ""

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(terraform, "run_terraform", side_effect=run),
        ):
            with self.assertRaisesRegex(EvalveError, "would delete the database"):
                terraform.apply(aws, Path(directory), {"account_id": ACCOUNT})
        self.assertEqual(commands, ["init", "plan", "show"])
