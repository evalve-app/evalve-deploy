# The agent images of the Loop tasks (ADR 0006 point 13). The Evalve CLI makes
# the ECR repository, not Terraform, so that it stays after `evalve down`: a
# restore needs the images. Terraform only names the repository here.
locals {
  agent_image_repository     = "evalve-agent-images"
  agent_image_repository_arn = "arn:aws:ecr:${var.region}:${var.account_id}:repository/${local.agent_image_repository}"
  agent_image_repository_url = "${var.account_id}.dkr.ecr.${var.region}.amazonaws.com/${local.agent_image_repository}"
}

# The backend assumes this role to give a builder an ECR token. The role can
# push an image and cannot delete one.
resource "aws_iam_role" "agent_image_push" {
  count       = local.loop_task_enabled ? 1 : 0
  name_prefix = "evalve-agent-image-push-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.server.arn }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "agent_image_push" {
  count = local.loop_task_enabled ? 1 : 0
  role  = aws_iam_role.agent_image_push[0].name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
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
        Resource = local.agent_image_repository_arn
      },
    ]
  })
}
