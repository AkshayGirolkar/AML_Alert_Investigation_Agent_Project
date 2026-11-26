# ECS Fargate: one image, three task definitions (api, worker, migrate). Terraform creates
# the services; rollouts of new images are done by scripts/aws_deploy.sh (registers a new
# task definition revision), so the services ignore task_definition drift.

locals {
  image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag == "" ? "bootstrap" : var.image_tag}"
  deployed     = var.image_tag != ""
  rds_password = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::"

  # AWS_ENDPOINT_URL stays unset: the SDK uses real AWS and the task role.
  common_env = [
    { name = "POSTGRES_HOST", value = aws_db_instance.main.address },
    { name = "POSTGRES_PORT", value = "5432" },
    { name = "POSTGRES_USER", value = var.db_username },
    { name = "POSTGRES_DB", value = var.db_name },
    { name = "REDIS_URL", value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" },
    { name = "AWS_REGION", value = var.region },
    { name = "SQS_QUEUE_NAME", value = var.sqs_queue_name },
    { name = "SQS_DLQ_NAME", value = var.sqs_dlq_name },
    { name = "SQS_VISIBILITY_TIMEOUT", value = tostring(var.sqs_visibility_timeout) },
    { name = "SQS_MAX_RECEIVE_COUNT", value = tostring(var.sqs_max_receive_count) },
    { name = "LOG_FORMAT", value = "json" },
    { name = "LLM_PROVIDER", value = var.llm_provider },
    { name = "LANGSMITH_TRACING", value = tostring(var.langsmith_tracing) },
    { name = "S3_DATA_BUCKET", value = aws_s3_bucket.data.bucket },
  ]

  common_secrets = [
    { name = "POSTGRES_PASSWORD", valueFrom = local.rds_password },
    { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
    { name = "LANGSMITH_API_KEY", valueFrom = aws_secretsmanager_secret.langsmith_api_key.arn },
    { name = "CONSOLE_DB_PASSWORD", valueFrom = aws_secretsmanager_secret.console_db_password.arn },
  ]
}

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled" # gives the RunningTaskCount metric used by worker autoscaling
  }
}

resource "aws_cloudwatch_log_group" "app" {
  for_each          = toset(["api", "worker", "migrate"])
  name              = "/ecs/${local.name}/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.image
    essential    = true
    command      = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment  = local.common_env
    secrets      = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["api"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image
    essential = true
    command   = ["python", "-m", "app.worker"]
    # SIGTERM stops polling and lets in-flight cases finish; Fargate allows up to 120 s.
    stopTimeout = 120
    environment = concat(local.common_env, [
      { name = "WORKER_CONCURRENCY", value = tostring(var.worker_concurrency) },
      { name = "WORKER_DB_POOL_SIZE", value = "0" },
      { name = "WORKER_METRICS_PORT", value = "9100" },
    ])
    secrets = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["worker"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

# One-shot: `aws ecs run-task` runs Alembic (creates vector/pg_trgm via migration 0002).
# Also the base for seed tasks (command overridden at run time).
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name        = "migrate"
    image       = local.image
    essential   = true
    command     = ["alembic", "upgrade", "head"]
    environment = local.common_env
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app["migrate"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = local.deployed ? var.api_min_tasks : 0
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http]

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_ecs_service" "worker" {
  name            = "worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = local.deployed ? var.worker_min_tasks : 0
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}
