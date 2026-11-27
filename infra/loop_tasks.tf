# Where the Loop tasks run (ADR 0006 point 13). The backend registers the task
# definitions, one family for each agent image, so Terraform has none. A Loop
# task has no task role.
resource "aws_ecs_cluster" "evalve" {
  count = local.loop_task_enabled ? 1 : 0
  name  = "evalve"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

# ECS uses this role to pull the agent image and to write the logs. It reads
# no secret.
resource "aws_iam_role" "loop_task_execution" {
  count       = local.loop_task_enabled ? 1 : 0
  name_prefix = "evalve-loop-task-execution-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "loop_task_execution" {
  count      = local.loop_task_enabled ? 1 : 0
  role       = aws_iam_role.loop_task_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Terraform makes the log group, because the execution role cannot.
# The retention of 7 days is a placeholder.
resource "aws_cloudwatch_log_group" "loop_task" {
  count             = local.loop_task_enabled ? 1 : 0
  name              = "/evalve/loop-task"
  retention_in_days = 7
}
