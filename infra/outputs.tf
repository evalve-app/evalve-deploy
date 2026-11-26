output "url" { value = local.url }
output "dashboard_url" { value = "https://dashboard.${local.hostname}" }
output "public_ip" { value = aws_eip.server.public_ip }
output "instance_id" { value = aws_instance.server.id }
output "database_id" { value = aws_db_instance.main.identifier }

output "deployment" {
  description = "Non-secret settings for subsequent Evalve operations."
  value = {
    domain                     = var.domain
    server_instance_type       = var.server_instance_type
    database_instance_class    = var.database_instance_class
    backup_retention_days      = var.backup_retention_days
    application_secret_arn     = var.application_secret_arn
    application_secret_version = var.application_secret_version
    database_engine_version    = var.database_engine_version
    server_image               = var.server_image
    backend_image              = var.backend_image
    backend_image_pinned       = var.backend_image_pinned
    loop_task_egress_ports     = var.loop_task_egress_ports
    snapshot_id                = var.snapshot_id
  }
}

# Where a Loop task runs and where an agent image goes. Null with no backend.
output "loop_task_subnet_id" {
  value = local.loop_task_enabled ? aws_subnet.server.id : null
}

output "loop_task_security_group_id" {
  value = one(aws_security_group.loop_task[*].id)
}

output "agent_image_repository_url" {
  value = local.loop_task_enabled ? local.agent_image_repository_url : null
}

output "agent_image_push_role_arn" {
  value = one(aws_iam_role.agent_image_push[*].arn)
}
