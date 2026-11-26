variable "account_id" {
  type        = string
  description = "Expected AWS account ID."

  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Provide a 12-digit AWS account ID."
  }
}

variable "region" {
  type        = string
  description = "AWS region for the deployment."
}

variable "server_instance_type" {
  type        = string
  description = "EC2 instance type compatible with the x86-64 Amazon Linux AMI."
}

variable "database_instance_class" {
  type        = string
  description = "RDS instance class for the single-AZ PostgreSQL database."
}

variable "backup_retention_days" {
  type        = number
  default     = 7
  nullable    = false
  description = "Days of RDS automated backup retention."

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 35 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "Backup retention must be a whole number between 1 and 35 days."
  }
}

variable "application_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for Kitaru's signing key, encryption key, and bootstrap password."

  validation {
    condition     = startswith(var.application_secret_arn, "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:")
    error_message = "The application secret must belong to the deployment account and region."
  }
}

variable "domain" {
  type        = string
  default     = null
  description = "Optional custom DNS name with an A record pointing to the Elastic IP."

  validation {
    condition     = var.domain == null || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.domain))
    error_message = "Use a lowercase DNS hostname, or leave domain unset to use sslip.io."
  }
}

variable "snapshot_id" {
  type    = string
  default = null
}

variable "database_engine_version" {
  type    = string
  default = "16"
}

variable "server_image" {
  type    = string
  default = "zenmldocker/kitaru-server@sha256:05ea00482c0adf4609a34d2c95575a6530ce5d60f6cea0e79716336d9e251d47" # 0.27.6

  validation {
    condition     = can(regex("^zenmldocker/kitaru-server@sha256:[a-f0-9]{64}$", var.server_image))
    error_message = "Use an immutable Kitaru server image digest."
  }
}

variable "backend_image" {
  type        = string
  default     = ""
  nullable    = false
  description = "Evalve backend image pinned by digest. Empty means no backend."

  validation {
    condition     = var.backend_image == "" || can(regex("^ghcr\\.io/evalve-app/evalve-backend@sha256:[a-f0-9]{64}$", var.backend_image))
    error_message = "Use an immutable Evalve backend image digest such as ghcr.io/evalve-app/evalve-backend@sha256:<64 hex characters>, or leave backend_image empty."
  }
}

variable "backend_secret_arn" {
  type        = string
  default     = ""
  nullable    = false
  description = "Secrets Manager ARN of the Backend secret: the PostgreSQL role and password of the Evalve backend. Empty when no backend was ever deployed."

  # No rule that a backend image needs this ARN: settings saved by an older
  # Evalve have a backend image and no ARN, and down must still tear them down.
  validation {
    condition     = var.backend_secret_arn == "" || startswith(var.backend_secret_arn, "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:")
    error_message = "The backend secret must belong to the deployment account and region."
  }
}

variable "backend_image_pinned" {
  type        = bool
  default     = false
  nullable    = false
  description = "True when the Operator chose backend_image by hand. Evalve then keeps it instead of the repo default."
}

variable "loop_task_egress_ports" {
  type        = list(number)
  default     = []
  nullable    = false
  description = "Extra TCP ports that a Loop task can reach on any address. Port 443 is always open."

  validation {
    condition = alltrue([
      for port in var.loop_task_egress_ports :
      port >= 1 && port <= 65535 && floor(port) == port && port != 443
    ])
    error_message = "Each Loop task port must be a whole number from 1 to 65535, and not 443, which is always open."
  }
}

variable "application_secret_version" {
  type    = string
  default = null
}

variable "verified_shutdown" {
  type    = bool
  default = false
}
