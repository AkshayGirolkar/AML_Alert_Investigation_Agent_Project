# RDS Postgres 17 and ElastiCache Redis 7, private subnets only.

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "pg17" {
  name   = "${local.name}-pg17"
  family = "postgres17"

  # SSL is already forced by default on PG15+; stated so it is explicit and reviewable.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "17"
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage_gb
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = var.db_name
  username = var.db_username
  # RDS creates and stores the master password in Secrets Manager; it never enters
  # Terraform state or tfvars. ECS reads it with the `<arn>:password::` selector.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg17.name
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period      = 1
  performance_insights_enabled = true
  auto_minor_version_upgrade   = true
  apply_immediately            = true

  deletion_protection       = !var.destroyable
  skip_final_snapshot       = var.destroyable
  final_snapshot_identifier = var.destroyable ? null : "${local.name}-final"
}

resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.name
  description          = "AML agent Redis (sanctions cache, rate limiter, locks, SSE)"

  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  port                 = 6379
  num_cache_clusters   = 1
  parameter_group_name = "default.redis7"

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  # TLS: the app uses REDIS_URL=rediss://<endpoint>:6379/0.
  transit_encryption_enabled = true
  at_rest_encryption_enabled = true
  automatic_failover_enabled = false
  apply_immediately          = true
}
