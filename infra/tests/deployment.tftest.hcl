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
