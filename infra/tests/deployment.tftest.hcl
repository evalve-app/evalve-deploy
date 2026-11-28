mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1b", "us-east-1a"] }
  }

  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }

  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10" }
  }

  mock_resource "aws_db_instance" {
    defaults = {
      address = "evalve.example.us-east-1.rds.amazonaws.com"
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds-test"
        kms_key_id    = "test"
        secret_status = "active"
      }]
    }
  }
}

variables {
  account_id              = "123456789012"
  region                  = "us-east-1"
  application_secret_arn  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:evalve-test"
  server_instance_type    = "t3.small"
  database_instance_class = "db.t4g.micro"
}

run "dedicated_network" {
  command = apply

  assert {
    condition = (
      aws_subnet.server.vpc_id == aws_vpc.main.id &&
      aws_route_table_association.server.subnet_id == aws_subnet.server.id &&
      aws_instance.server.subnet_id == aws_subnet.server.id
    )
    error_message = "Only the server subnet should use the public route table."
  }

  assert {
    condition = (
      length(aws_subnet.database) == 2 &&
      length(toset(aws_subnet.database[*].availability_zone)) == 2 &&
      aws_db_subnet_group.main.subnet_ids == toset(aws_subnet.database[*].id) &&
      alltrue([for subnet in aws_subnet.database : subnet.vpc_id == aws_vpc.main.id && !subnet.map_public_ip_on_launch])
    )
    error_message = "The DB subnet group must span two private subnets in different Availability Zones."
  }

  assert {
    condition = (
      aws_db_instance.main.availability_zone == aws_subnet.server.availability_zone &&
      !aws_db_instance.main.multi_az && !aws_db_instance.main.publicly_accessible &&
      aws_db_instance.main.storage_encrypted && aws_db_instance.main.manage_master_user_password &&
      aws_db_instance.main.backup_retention_period == 7 && aws_db_instance.main.deletion_protection &&
      !aws_db_instance.main.skip_final_snapshot
    )
    error_message = "The database must be private, single-AZ, encrypted, and protected against accidental deletion."
  }

  assert {
    condition = (
      toset([for rule in aws_vpc_security_group_ingress_rule.web : rule.from_port]) == toset([80, 443]) &&
      aws_vpc_security_group_ingress_rule.postgres.referenced_security_group_id == aws_security_group.server.id &&
      aws_vpc_security_group_ingress_rule.postgres.cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.postgres.from_port == 5432
    )
    error_message = "Public ingress must be limited to HTTP and HTTPS, with PostgreSQL reachable only from EC2."
  }

  assert {
    condition = (
      !aws_instance.server.associate_public_ip_address &&
      aws_eip_association.server.allocation_id == aws_eip.server.id &&
      aws_eip_association.server.instance_id == aws_instance.server.id &&
      aws_instance.server.metadata_options[0].http_tokens == "required" &&
      aws_instance.server.root_block_device[0].encrypted
    )
    error_message = "The instance must use its Elastic IP, IMDSv2, and encrypted EBS storage."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.secrets.policy).Statement[0].Resource) == toset([
      var.application_secret_arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ])
    error_message = "Without a backend, the server role must read only the Application and Database secrets."
  }

  assert {
    # Compressed, so the user data stays under the EC2 limit of 16 KB.
    condition = (
      strcontains(aws_instance.server.user_data, "echo '${base64gzip(file("refresh.py"))}' | base64 -d | gunzip > /etc/evalve/refresh.py\n") &&
      length(aws_instance.server.user_data) < 16384
    )
    error_message = "The server must get refresh.py compressed, and the user data must stay under 16 KB."
  }

  assert {
    condition = (
      output.url == "https://203-0-113-10.sslip.io" &&
      strcontains(aws_instance.server.user_data, "203-0-113-10.sslip.io {") &&
      strcontains(aws_instance.server.user_data, "reverse_proxy 127.0.0.1:8000")
    )
    error_message = "Caddy must serve the Elastic IP's sslip.io hostname and proxy to Kitaru on loopback."
  }
}

run "custom_domain" {
  command = apply

  variables {
    domain = "evalve.example.com"
  }

  assert {
    condition = (
      output.url == "https://evalve.example.com" && output.public_ip == "203.0.113.10" &&
      strcontains(aws_instance.server.user_data, "evalve.example.com {") &&
      !strcontains(aws_instance.server.user_data, "203-0-113-10.sslip.io {")
    )
    error_message = "A custom hostname should serve the same Elastic IP and replace the default hostname."
  }

  assert {
    condition = (
      output.dashboard_url == "https://dashboard.evalve.example.com" &&
      strcontains(aws_instance.server.user_data, "dashboard.evalve.example.com {") &&
      strcontains(aws_instance.server.user_data, "header_up Host evalve.example.com")
    )
    error_message = "With a custom hostname, the dashboard must live at dashboard.<domain>."
  }
}

run "free_plan_uses_one_day_of_automated_backups" {
  command = apply

  variables {
    backup_retention_days = 1
  }

  assert {
    condition     = aws_db_instance.main.backup_retention_period == 1 && output.deployment.backup_retention_days == 1
    error_message = "The Free plan override must reach RDS and be recorded in deployment state."
  }
}

run "no_backend_image_records_an_empty_image" {
  command = apply

  assert {
    condition     = output.deployment.backend_image == ""
    error_message = "A deployment without backend_image must record an empty image."
  }

  assert {
    condition     = !strcontains(aws_instance.server.user_data, "evalve-backend")
    error_message = "Without backend_image, the server must have no backend unit."
  }

  assert {
    condition = (
      strcontains(aws_instance.server.user_data, "203-0-113-10.sslip.io {\n  reverse_proxy 127.0.0.1:8000\n}") &&
      !strcontains(aws_instance.server.user_data, "/evalve/*") &&
      !strcontains(aws_instance.server.user_data, "127.0.0.1:8100")
    )
    error_message = "Without backend_image, Caddy must send every path to Kitaru as before."
  }

  assert {
    condition = (
      strcontains(aws_instance.server.user_data, "import /etc/caddy/sites/*.caddy") &&
      strcontains(aws_instance.server.user_data, "install -d -m 755 /etc/evalve/sites") &&
      strcontains(aws_instance.server.user_data, "--mount type=bind,src=/etc/evalve/sites,dst=/etc/caddy/sites,readonly")
    )
    error_message = "Caddy must always import the sites folder that evalve-ui writes its dashboard into."
  }

  assert {
    # evalve-ui's remote-install.sh uses this exact line as an anchor (ADR 0003).
    condition     = strcontains(aws_instance.server.user_data, "  --mount type=bind,src=/etc/evalve/Caddyfile,dst=/etc/caddy/Caddyfile,readonly \\\n")
    error_message = "The Caddyfile mount line is part of the contract with evalve-ui and must not change."
  }

  assert {
    condition = (
      strcontains(aws_instance.server.user_data, "install -d -m 755 /etc/evalve/sites /etc/evalve/dashboard") &&
      strcontains(aws_instance.server.user_data, "--mount type=bind,src=/etc/evalve/dashboard,dst=/srv/dashboard,readonly")
    )
    error_message = "Bootstrap must create the dashboard folder before Caddy starts and mount it into Caddy."
  }

  assert {
    condition = strcontains(aws_instance.server.user_data, join("\n", [
      "dashboard.203-0-113-10.sslip.io {",
      "  encode gzip",
      "  handle /api/* {",
      "    reverse_proxy 127.0.0.1:8000 {",
      "      header_up Host 203-0-113-10.sslip.io",
      "    }",
      "  }",
      "  handle {",
    ]))
    error_message = "The dashboard site must send /api/* to Kitaru with Kitaru's hostname, and no /evalve/* route without a backend."
  }

  assert {
    condition = (
      strcontains(aws_instance.server.user_data, "root * /srv/dashboard/current") &&
      strcontains(aws_instance.server.user_data, "try_files {path} /index.html") &&
      strcontains(aws_instance.server.user_data, "header @assets Cache-Control \"public, max-age=31536000, immutable\"") &&
      strcontains(aws_instance.server.user_data, "header @pages Cache-Control \"no-cache\"")
    )
    error_message = "The dashboard site must serve the current release as a single-page app, with cache headers."
  }

  assert {
    condition     = output.dashboard_url == "https://dashboard.203-0-113-10.sslip.io"
    error_message = "The dashboard URL must be dashboard.<hostname>."
  }
}

run "backend_image_is_recorded_for_restore" {
  command = apply

  variables {
    backend_image        = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    backend_image_pinned = true
    backend_secret_arn   = "arn:aws:secretsmanager:us-east-1:123456789012:secret:evalve/backend-test"
  }

  assert {
    condition     = output.deployment.backend_image == var.backend_image
    error_message = "The backend image must be recorded in deployment state so down and restore keep it."
  }

  assert {
    condition     = output.deployment.backend_image_pinned == true
    error_message = "The pinned marker must be recorded so down and restore keep a hand-picked image."
  }

  assert {
    # Decode config.json from the user data, as the server does on boot.
    condition = jsondecode(base64decode(regex(
      "echo '([A-Za-z0-9+/=]+)' \\| base64 -d > /etc/evalve/config\\.json",
      aws_instance.server.user_data,
    )[0])).backend_secret_arn == var.backend_secret_arn
    error_message = "With backend_image, the server config must name the Backend secret."
  }

  assert {
    condition     = output.deployment.backend_secret_arn == var.backend_secret_arn
    error_message = "The Backend secret must be recorded in deployment state so up, down and restore reuse it."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.secrets.policy).Statement[0].Resource) == toset([
      var.application_secret_arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
      var.backend_secret_arn,
    ])
    error_message = "With a backend, the server role must read exactly the Application, Database and Backend secrets."
  }

  assert {
    condition = (
      strcontains(aws_instance.server.user_data, "cat > /etc/systemd/system/evalve-backend.service") &&
      strcontains(aws_instance.server.user_data, "--name evalve-backend --network host") &&
      strcontains(aws_instance.server.user_data, "evalve-caddy.service evalve-backend.service")
    )
    error_message = "With backend_image, the server must run and enable the backend on the host network."
  }

  assert {
    # A new image must not change user_data, or Terraform replaces EC2.
    condition = (
      !strcontains(aws_instance.server.user_data, var.backend_image) &&
      strcontains(aws_instance.server.user_data, "ConditionPathExists=/etc/evalve/backend-image.env") &&
      strcontains(aws_instance.server.user_data, "EnvironmentFile=/etc/evalve/backend-image.env") &&
      strcontains(aws_instance.server.user_data, "--env EVALVE_IMAGE=$${EVALVE_BACKEND_IMAGE} \\") &&
      strcontains(aws_instance.server.user_data, "  $${EVALVE_BACKEND_IMAGE}\n")
    )
    error_message = "The backend unit must read its image from the file that up writes, not from user_data."
  }

  assert {
    condition = strcontains(aws_instance.server.user_data, join("\n", [
      "203-0-113-10.sslip.io {",
      "  handle /evalve/* {",
      "    reverse_proxy 127.0.0.1:8100",
      "  }",
      "  handle {",
      "    reverse_proxy 127.0.0.1:8000",
      "  }",
      "}",
    ]))
    error_message = "With backend_image, Caddy must send /evalve/* to the backend and everything else to Kitaru."
  }

  assert {
    condition = strcontains(aws_instance.server.user_data, join("\n", [
      "    }",
      "  }",
      "  handle /evalve/* {",
      "    reverse_proxy 127.0.0.1:8100",
      "  }",
      "  handle {",
      "    # Vite names asset files by their content, so they can be cached forever.",
    ]))
    error_message = "With backend_image, the dashboard site must also send /evalve/* to the backend."
  }
}

run "reject_backend_image_tag" {
  command = plan

  variables {
    backend_image      = "ghcr.io/evalve-app/evalve-backend:v0.1.0"
    backend_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:evalve/backend-test"
  }

  expect_failures = [var.backend_image]
}

run "no_backend_keeps_a_saved_backend_secret_out_of_the_server_config" {
  command = apply

  variables {
    backend_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:evalve/backend-test"
  }

  assert {
    condition = !contains(keys(jsondecode(base64decode(regex(
      "echo '([A-Za-z0-9+/=]+)' \\| base64 -d > /etc/evalve/config\\.json",
      aws_instance.server.user_data,
    )[0]))), "backend_secret_arn")
    error_message = "Without backend_image, the server config must not name the Backend secret, so the user data stays the same."
  }

  assert {
    condition     = output.deployment.backend_secret_arn == var.backend_secret_arn
    error_message = "With the backend off, the saved Backend secret must stay in deployment state for a later backend."
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.secrets.policy).Statement[0].Resource) == toset([
      var.application_secret_arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ])
    error_message = "With the backend off, the server role must not read the saved Backend secret."
  }
}

run "reject_foreign_backend_secret" {
  command = plan

  variables {
    backend_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:other-account"
  }

  expect_failures = [var.backend_secret_arn]
}

run "older_settings_with_a_backend_and_no_secret_still_work" {
  # Settings saved by an older Evalve, which down and restore pass on.
  command = apply

  variables {
    backend_image = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition = toset(jsondecode(aws_iam_role_policy.secrets.policy).Statement[0].Resource) == toset([
      var.application_secret_arn,
      aws_db_instance.main.master_user_secret[0].secret_arn,
    ])
    error_message = "Without a Backend secret ARN, the server role must read only the Application and Database secrets."
  }

  assert {
    condition = !contains(keys(jsondecode(base64decode(regex(
      "echo '([A-Za-z0-9+/=]+)' \\| base64 -d > /etc/evalve/config\\.json",
      aws_instance.server.user_data,
    )[0]))), "backend_secret_arn")
    error_message = "Without a Backend secret ARN, the server config must not name one."
  }
}

run "reject_foreign_secret" {
  command = plan

  variables {
    application_secret_arn = "arn:aws:secretsmanager:us-east-1:111111111111:secret:other-account"
  }

  expect_failures = [var.application_secret_arn]
}

run "restore_keeps_database_private_and_pins_application_secret" {
  command = apply

  variables {
    snapshot_id                = "evalve-20261110170000-123456789abc"
    database_engine_version    = "16.12"
    application_secret_version = "12345678-1234-1234-1234-123456789abc"
  }

  assert {
    condition = (
      aws_db_instance.main.snapshot_identifier == var.snapshot_id &&
      aws_db_instance.main.engine_version == var.database_engine_version &&
      aws_db_instance.main.manage_master_user_password &&
      aws_db_instance.main.deletion_protection &&
      !aws_db_instance.main.publicly_accessible &&
      output.deployment.application_secret_version == var.application_secret_version
    )
    error_message = "Restoration must use the saved snapshot and credential version, with a private, protected RDS instance."
  }
}

run "loop_task_security_group_allows_only_https_egress_by_default" {
  command = apply

  variables {
    backend_image = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition = (
      length(aws_security_group.loop_task) == 1 &&
      startswith(aws_security_group.loop_task[0].name_prefix, "evalve-loop-task-") &&
      aws_security_group.loop_task[0].vpc_id == aws_vpc.main.id
    )
    error_message = "With a backend, the Loop task must have its own security group in the Evalve VPC."
  }

  assert {
    condition = alltrue([
      for rule in concat(values(aws_vpc_security_group_ingress_rule.web), [aws_vpc_security_group_ingress_rule.postgres]) :
      rule.security_group_id != aws_security_group.loop_task[0].id
    ])
    error_message = "The security group of the Loop task must have no ingress rule."
  }

  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.loop_task) == 1 &&
      alltrue([
        for rule in aws_vpc_security_group_egress_rule.loop_task :
        rule.security_group_id == aws_security_group.loop_task[0].id &&
        rule.cidr_ipv4 == "0.0.0.0/0" && rule.ip_protocol == "tcp" &&
        rule.from_port == 443 && rule.to_port == 443
      ])
    )
    error_message = "By default, the only egress of the Loop task is TCP 443 to any address."
  }

  assert {
    condition = alltrue([
      for rule in concat(
        values(aws_vpc_security_group_ingress_rule.web),
        [
          aws_vpc_security_group_ingress_rule.postgres,
          aws_vpc_security_group_egress_rule.https,
          aws_vpc_security_group_egress_rule.postgres,
        ],
      ) : rule.referenced_security_group_id != aws_security_group.loop_task[0].id
    ])
    error_message = "No rule of a different security group may name the security group of the Loop task."
  }

  assert {
    # The two asserts above name each rule. A new rule resource must be added to them.
    condition = toset(flatten([
      for name in fileset(path.module, "*.tf") :
      regexall("resource \"aws_vpc_security_group_(?:in|e)gress_rule\" \"[a-z_]+\"", file("${path.module}/${name}"))
      ])) == toset([
      "resource \"aws_vpc_security_group_ingress_rule\" \"web\"",
      "resource \"aws_vpc_security_group_ingress_rule\" \"postgres\"",
      "resource \"aws_vpc_security_group_egress_rule\" \"https\"",
      "resource \"aws_vpc_security_group_egress_rule\" \"postgres\"",
      "resource \"aws_vpc_security_group_egress_rule\" \"loop_task\"",
    ]) && length(regexall("(?m)^\\s*(?:in|e)gress\\s*[{=]", join("\n", [for name in fileset(path.module, "*.tf") : file("${path.module}/${name}")]))) == 0
    error_message = "A security group rule was added or removed. Check it against the Loop task asserts in this run, then update this list."
  }
}

run "loop_task_egress_ports_add_one_rule_each" {
  command = apply

  variables {
    backend_image          = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    loop_task_egress_ports = [5432]
  }

  assert {
    condition = (
      length(aws_vpc_security_group_egress_rule.loop_task) == 2 &&
      toset([for rule in aws_vpc_security_group_egress_rule.loop_task : rule.from_port]) == toset([443, 5432]) &&
      alltrue([
        for rule in aws_vpc_security_group_egress_rule.loop_task :
        rule.from_port == rule.to_port && rule.ip_protocol == "tcp" && rule.cidr_ipv4 == "0.0.0.0/0"
      ])
    )
    error_message = "Each port of loop_task_egress_ports must add one TCP egress rule to any address."
  }

  assert {
    condition     = output.deployment.loop_task_egress_ports == tolist([5432])
    error_message = "The extra ports must be recorded in deployment state so down and restore keep them."
  }
}

run "reject_loop_task_port_443" {
  command = plan

  variables {
    loop_task_egress_ports = [443]
  }

  expect_failures = [var.loop_task_egress_ports]
}

run "reject_loop_task_port_zero" {
  command = plan

  variables {
    loop_task_egress_ports = [0]
  }

  expect_failures = [var.loop_task_egress_ports]
}

run "reject_loop_task_port_above_65535" {
  command = plan

  variables {
    loop_task_egress_ports = [70000]
  }

  expect_failures = [var.loop_task_egress_ports]
}

run "reject_loop_task_port_that_is_not_whole" {
  command = plan

  variables {
    loop_task_egress_ports = [5432.5]
  }

  expect_failures = [var.loop_task_egress_ports]
}

run "agent_image_push_role_can_only_push_to_the_one_repository" {
  command = apply

  variables {
    backend_image = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition = (
      length(aws_iam_role.agent_image_push) == 1 &&
      startswith(aws_iam_role.agent_image_push[0].name_prefix, "evalve-agent-image-push-")
    )
    error_message = "With a backend, the deployment must have the push-only role."
  }

  assert {
    condition = (
      length(jsondecode(aws_iam_role.agent_image_push[0].assume_role_policy).Statement) == 1 &&
      jsondecode(aws_iam_role.agent_image_push[0].assume_role_policy).Statement[0] == {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.server.arn }
        Action    = "sts:AssumeRole"
      }
    )
    error_message = "Only the instance role may assume the push-only role."
  }

  assert {
    condition = (
      length(aws_iam_role_policy.agent_image_push) == 1 &&
      aws_iam_role_policy.agent_image_push[0].role == aws_iam_role.agent_image_push[0].name &&
      jsondecode(aws_iam_role_policy.agent_image_push[0].policy).Statement == [
        {
          Effect   = "Allow"
          Action   = "ecr:GetAuthorizationToken"
          Resource = "*"
        },
        {
          Effect = "Allow"
          Action = [
            "ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart",
            "ecr:CompleteLayerUpload",
            "ecr:BatchCheckLayerAvailability",
            "ecr:PutImage",
            "ecr:BatchGetImage",
          ]
          Resource = "arn:aws:ecr:us-east-1:123456789012:repository/evalve-agent-images"
        },
      ]
    )
    error_message = "The push-only role may get a token, and push to and read evalve-agent-images of this account and region. Nothing else."
  }

  assert {
    condition     = !strcontains(lower(aws_iam_role_policy.agent_image_push[0].policy), "delete")
    error_message = "The push-only role must have no delete action."
  }

  assert {
    condition = alltrue([
      for name in fileset(path.module, "*.tf") : !strcontains(file("${path.module}/${name}"), "aws_ecr_repository")
    ])
    error_message = "Terraform must not manage the agent image repository. The CLI makes it, so that it stays after down."
  }

  assert {
    condition = (
      output.loop_task_subnet_id == aws_subnet.server.id &&
      output.loop_task_security_group_id == aws_security_group.loop_task[0].id &&
      output.agent_image_repository_url == "123456789012.dkr.ecr.us-east-1.amazonaws.com/evalve-agent-images" &&
      output.agent_image_push_role_arn == aws_iam_role.agent_image_push[0].arn
    )
    error_message = "With a backend, the deployment must output where a Loop task runs and where an agent image goes."
  }
}

run "loop_task_cluster_execution_role_and_log_group" {
  command = apply

  variables {
    backend_image = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition = (
      length(aws_ecs_cluster.evalve) == 1 &&
      aws_ecs_cluster.evalve[0].name == "evalve" &&
      [for setting in aws_ecs_cluster.evalve[0].setting : setting if setting.name == "containerInsights"] == [{ name = "containerInsights", value = "disabled" }]
    )
    error_message = "With a backend, the deployment must have the ECS cluster evalve, with Container Insights off."
  }

  assert {
    condition = (
      length(aws_cloudwatch_log_group.loop_task) == 1 &&
      aws_cloudwatch_log_group.loop_task[0].name == "/evalve/loop-task" &&
      aws_cloudwatch_log_group.loop_task[0].retention_in_days == 7
    )
    error_message = "With a backend, Terraform must make the log group /evalve/loop-task, with a retention of 7 days."
  }

  assert {
    condition = (
      length(aws_iam_role.loop_task_execution) == 1 &&
      startswith(aws_iam_role.loop_task_execution[0].name_prefix, "evalve-loop-task-execution-") &&
      jsondecode(aws_iam_role.loop_task_execution[0].assume_role_policy).Statement == [{
        Effect    = "Allow"
        Principal = { Service = "ecs-tasks.amazonaws.com" }
        Action    = "sts:AssumeRole"
      }]
    )
    error_message = "With a backend, only ecs-tasks.amazonaws.com may assume the execution role of the Loop task."
  }

  assert {
    condition = (
      length(aws_iam_role_policy_attachment.loop_task_execution) == 1 &&
      aws_iam_role_policy_attachment.loop_task_execution[0].role == aws_iam_role.loop_task_execution[0].name &&
      aws_iam_role_policy_attachment.loop_task_execution[0].policy_arn == "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy" &&
      alltrue([
        for role in concat([aws_iam_role_policy_attachment.ssm.role, aws_iam_role_policy.secrets.role], aws_iam_role_policy.agent_image_push[*].role, aws_iam_role_policy.loop_tasks[*].role) :
        role != aws_iam_role.loop_task_execution[0].name
      ])
    )
    error_message = "The execution role must have AmazonECSTaskExecutionRolePolicy and no other policy."
  }

  assert {
    # The assert above names each policy resource. A new one must be added to it.
    condition = toset(flatten([
      for name in fileset(path.module, "*.tf") :
      regexall("resource \"aws_iam_(?:role_polic[a-z_]*|policy_attachment)\" \"[a-z_]+\"", file("${path.module}/${name}"))
      ])) == toset([
      "resource \"aws_iam_role_policy_attachment\" \"ssm\"",
      "resource \"aws_iam_role_policy\" \"secrets\"",
      "resource \"aws_iam_role_policy\" \"agent_image_push\"",
      "resource \"aws_iam_role_policy_attachment\" \"loop_task_execution\"",
      "resource \"aws_iam_role_policy\" \"loop_tasks\"",
    ]) && length(regexall("(?m)^\\s*(?:managed_policy_arns|inline_policy)\\s*[{=]", join("\n", [for name in fileset(path.module, "*.tf") : file("${path.module}/${name}")]))) == 0
    error_message = "An IAM policy resource was added or removed. Check it against the execution role assert in this run, then update this list."
  }

  assert {
    condition = alltrue([
      for name in fileset(path.module, "*.tf") : !strcontains(file("${path.module}/${name}"), "aws_ecs_task_definition")
    ])
    error_message = "Terraform must not manage a task definition. The backend registers one for each agent image."
  }

  assert {
    condition = (
      output.ecs_cluster_name == "evalve" &&
      output.loop_task_execution_role_arn == aws_iam_role.loop_task_execution[0].arn &&
      output.loop_task_log_group == "/evalve/loop-task"
    )
    error_message = "With a backend, the deployment must output the cluster, the execution role and the log group of the Loop task."
  }
}

run "instance_role_runs_loop_tasks_and_gives_push_credentials" {
  command = apply

  variables {
    backend_image = "ghcr.io/evalve-app/evalve-backend@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition = (
      length(aws_iam_role_policy.loop_tasks) == 1 &&
      aws_iam_role_policy.loop_tasks[0].role == aws_iam_role.server.name
    )
    error_message = "With a backend, the instance role must have the one policy for Loop tasks and agent images."
  }

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.loop_tasks[0].policy).Statement : statement if statement.Effect == "Allow"
      ] == [
      {
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = "arn:aws:ecs:us-east-1:123456789012:task-definition/evalve-loop-task-*:*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.evalve[0].arn } }
      },
      {
        Effect    = "Allow"
        Action    = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource  = "arn:aws:ecs:us-east-1:123456789012:task/evalve/*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.evalve[0].arn } }
      },
      {
        Effect    = "Allow"
        Action    = "ecs:TagResource"
        Resource  = "arn:aws:ecs:us-east-1:123456789012:task/evalve/*"
        Condition = { StringEquals = { "ecs:CreateAction" = "RunTask" } }
      },
      {
        Effect   = "Allow"
        Action   = "ecs:RegisterTaskDefinition"
        Resource = "arn:aws:ecs:us-east-1:123456789012:task-definition/evalve-loop-task-*:*"
      },
      {
        Effect   = "Allow"
        Action   = "ecs:DescribeTaskDefinition"
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.loop_task_execution[0].arn
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
      {
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.agent_image_push[0].arn
      },
      {
        Effect   = "Allow"
        Action   = "ecr:DescribeImages"
        Resource = "arn:aws:ecr:us-east-1:123456789012:repository/evalve-agent-images"
      },
    ]
    error_message = "The instance role may run, stop, describe and tag Loop tasks of the Evalve cluster, register and describe task definitions, pass the execution role to ECS tasks, assume the push-only role, and describe images of evalve-agent-images. Nothing else."
  }

  assert {
    condition = [
      for statement in jsondecode(aws_iam_role_policy.loop_tasks[0].policy).Statement : statement if statement.Effect != "Allow"
      ] == [{
        Effect    = "Deny"
        Action    = "ecs:RunTask"
        Resource  = "*"
        Condition = { StringEquals = { "ecs:enable-execute-command" = "true" } }
    }]
    error_message = "The instance role must not run a task with ECS Exec on."
  }

  assert {
    condition = alltrue([
      for word in ["ecs:ListTasks", "logs:", "s3:", "secretsmanager:"] :
      !strcontains(aws_iam_role_policy.loop_tasks[0].policy, word)
    ])
    error_message = "The policy for Loop tasks must not list tasks, and must have no logs, S3 or secret action."
  }
}

run "no_backend_has_no_loop_task_resources" {
  command = apply

  assert {
    condition = (
      length(aws_security_group.loop_task) == 0 &&
      length(aws_vpc_security_group_egress_rule.loop_task) == 0 &&
      length(aws_iam_role.agent_image_push) == 0 &&
      length(aws_iam_role_policy.agent_image_push) == 0 &&
      length(aws_ecs_cluster.evalve) == 0 &&
      length(aws_iam_role.loop_task_execution) == 0 &&
      length(aws_iam_role_policy_attachment.loop_task_execution) == 0 &&
      length(aws_iam_role_policy.loop_tasks) == 0 &&
      length(aws_cloudwatch_log_group.loop_task) == 0
    )
    error_message = "Without backend_image, none of the Loop task resources may exist."
  }

  assert {
    condition = (
      output.loop_task_subnet_id == null &&
      output.loop_task_security_group_id == null &&
      output.agent_image_repository_url == null &&
      output.agent_image_push_role_arn == null &&
      output.ecs_cluster_name == null &&
      output.loop_task_execution_role_arn == null &&
      output.loop_task_log_group == null
    )
    error_message = "Without backend_image, the seven Loop task outputs must be null."
  }
}

run "no_backend_ignores_saved_loop_task_ports" {
  command = apply

  variables {
    loop_task_egress_ports = [5432]
  }

  assert {
    condition     = length(aws_vpc_security_group_egress_rule.loop_task) == 0 && output.deployment.loop_task_egress_ports == tolist([5432])
    error_message = "Without backend_image, saved ports make no rule, and they stay in deployment state."
  }
}
