data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  hostname    = var.domain != null ? var.domain : "${replace(aws_eip.server.public_ip, ".", "-")}.sslip.io"
  url         = "https://${local.hostname}"
  caddy_image = "caddy@sha256:c2aeffaeb80d5403e9c8d1d908b50e191aad204e16129f991ae357ada642094d" # 2.11.9
}

resource "aws_eip" "server" {
  domain = "vpc"
  tags   = { Name = "evalve" }
}

resource "aws_eip_association" "server" {
  instance_id   = aws_instance.server.id
  allocation_id = aws_eip.server.id
}

resource "aws_iam_role" "server" {
  name_prefix = "evalve-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "secrets" {
  role = aws_iam_role.server.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      # Three named secrets with a backend, two without (ADR 0005 point 1).
      Resource = concat(
        [var.application_secret_arn, aws_db_instance.main.master_user_secret[0].secret_arn],
        var.backend_image != "" && var.backend_secret_arn != "" ? [var.backend_secret_arn] : [],
      )
    }]
  })
}

# What the backend does with the instance role: it starts and stops Loop tasks,
# registers their task definitions and gives a builder an ECR token (ADR 0006
# point 13, ADR 0004 point 8). ADR 0005 point 8 sets the limits on iam:PassRole
# and sts:AssumeRole. The role does not list tasks, and has no logs or S3 action.
locals {
  # The last * also matches the revision.
  loop_task_definition_arns = "arn:aws:ecs:${var.region}:${var.account_id}:task-definition/evalve-loop-task-*"
  loop_task_arns            = "arn:aws:ecs:${var.region}:${var.account_id}:task/${join("", aws_ecs_cluster.evalve[*].name)}/*"
  in_evalve_cluster         = { ArnEquals = { "ecs:cluster" = one(aws_ecs_cluster.evalve[*].arn) } }
}

resource "aws_iam_role_policy" "loop_tasks" {
  count = local.loop_task_enabled ? 1 : 0
  role  = aws_iam_role.server.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = local.loop_task_definition_arns
        Condition = local.in_evalve_cluster
      },
      {
        Effect    = "Allow"
        Action    = ["ecs:StopTask", "ecs:DescribeTasks"]
        Resource  = local.loop_task_arns
        Condition = local.in_evalve_cluster
      },
      {
        # RunTask with tags needs this. The backend sets evalve:loop and evalve:attempt.
        Effect    = "Allow"
        Action    = "ecs:TagResource"
        Resource  = local.loop_task_arns
        Condition = { StringEquals = { "ecs:CreateAction" = "RunTask" } }
      },
      {
        # The family prefix is a placeholder until a test on AWS confirms that
        # AWS applies it (sc-254).
        Effect   = "Allow"
        Action   = "ecs:RegisterTaskDefinition"
        Resource = local.loop_task_definition_arns
      },
      {
        # AWS has no resource for this action.
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
        Resource = local.agent_image_repository_arn
      },
      {
        # No ECS Exec: it opens a shell in a Loop task.
        Effect    = "Deny"
        Action    = "ecs:RunTask"
        Resource  = "*"
        Condition = { StringEquals = { "ecs:enable-execute-command" = "true" } }
      },
    ]
  })
}

resource "aws_iam_instance_profile" "server" {
  name_prefix = "evalve-"
  role        = aws_iam_role.server.name
}

resource "aws_instance" "server" {
  ami           = data.aws_ssm_parameter.ami.value
  instance_type = var.server_instance_type
  subnet_id     = aws_subnet.server.id
  # Don't set associate_public_ip_address. With the Elastic IP attached, AWS
  # reports a public IP, so Terraform would replace EC2 on every up. The
  # subnet already turns off automatic public IPs.
  vpc_security_group_ids      = [aws_security_group.server.id]
  iam_instance_profile        = aws_iam_instance_profile.server.name
  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/bootstrap.sh.tftpl", {
    # The Backend secret has no version, so the server always reads its newest
    # password (ADR 0005 point 4). It is in the config only with a backend, so
    # a deployment without one keeps the same user data and the same EC2.
    config = base64encode(jsonencode(merge(
      {
        region                     = var.region
        application_secret_arn     = var.application_secret_arn
        application_secret_version = var.application_secret_version
        database_secret_arn        = aws_db_instance.main.master_user_secret[0].secret_arn
        database_host              = aws_db_instance.main.address
        hostname                   = local.hostname
      },
      { for key, value in { backend_secret_arn = var.backend_secret_arn } : key => value if var.backend_image != "" && value != "" },
    )))
    refresh_script  = base64gzip(file("${path.module}/refresh.py"))
    hostname        = local.hostname
    public_ip       = aws_eip.server.public_ip
    kitaru_image    = var.server_image
    backend_enabled = var.backend_image != ""
    caddy_image     = local.caddy_image
  })

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  tags = { Name = "evalve" }

  depends_on = [
    aws_route_table_association.server,
    aws_iam_role_policy.secrets,
    aws_iam_role_policy_attachment.ssm,
    aws_vpc_security_group_egress_rule.https,
    aws_vpc_security_group_egress_rule.postgres,
    aws_vpc_security_group_ingress_rule.postgres,
    aws_vpc_security_group_ingress_rule.web,
  ]
}
