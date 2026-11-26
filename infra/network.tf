data "aws_availability_zones" "regional" {
  state = "available"

  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

locals {
  availability_zones = sort(data.aws_availability_zones.regional.names)
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "evalve" }
}

resource "aws_subnet" "server" {
  vpc_id            = aws_vpc.main.id
  availability_zone = local.availability_zones[0]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, 0)
  tags              = { Name = "evalve-public" }
}

resource "aws_subnet" "database" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  availability_zone       = local.availability_zones[count.index]
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 1)
  map_public_ip_on_launch = false
  tags                    = { Name = "evalve-db-${count.index + 1}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "server" {
  subnet_id      = aws_subnet.server.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "server" {
  name_prefix = "evalve-server-"
  description = "Caddy HTTP and HTTPS ingress"
  vpc_id      = aws_vpc.main.id
}

resource "aws_security_group" "database" {
  name_prefix = "evalve-db-"
  description = "PostgreSQL from the Evalve server"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "web" {
  for_each          = toset(["80", "443"])
  security_group_id = aws_security_group.server.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.server.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  security_group_id            = aws_security_group.server.id
  referenced_security_group_id = aws_security_group.database.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.server.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# The Loop task (ADR 0006 point 13) exists only when the deployment has a
# backend. It calls Kitaru and the backend at the public hostname, so it needs
# no rule in the security groups of the server and the database.
locals {
  loop_task_enabled      = var.backend_image != ""
  loop_task_egress_ports = local.loop_task_enabled ? toset([for port in concat([443], var.loop_task_egress_ports) : tostring(port)]) : toset([])
}

resource "aws_security_group" "loop_task" {
  count       = local.loop_task_enabled ? 1 : 0
  name_prefix = "evalve-loop-task-"
  description = "Loop tasks: no ingress, egress on HTTPS and the ports the Operator sets"
  vpc_id      = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "loop_task" {
  for_each          = local.loop_task_egress_ports
  security_group_id = aws_security_group.loop_task[0].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = tonumber(each.key)
  to_port           = tonumber(each.key)
}
