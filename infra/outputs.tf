# No secret values here. Passwords live in Secrets Manager.

output "vpc_id" { value = aws_vpc.main.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "app_security_group_id" { value = aws_security_group.app.id }
output "alb_security_group_id" { value = aws_security_group.alb.id }

output "rds_endpoint" { value = aws_db_instance.main.address }
output "rds_master_secret_arn" { value = aws_db_instance.main.master_user_secret[0].secret_arn }
output "redis_url" { value = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0" }

output "queue_url" { value = aws_sqs_queue.main.url }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
output "queue_arns" { value = [aws_sqs_queue.main.arn, aws_sqs_queue.dlq.arn] }

output "ecr_repository_url" { value = aws_ecr_repository.app.repository_url }
output "data_bucket" { value = aws_s3_bucket.data.bucket }

output "openai_secret_arn" { value = aws_secretsmanager_secret.openai_api_key.arn }
output "langsmith_secret_arn" { value = aws_secretsmanager_secret.langsmith_api_key.arn }
output "console_db_secret_arn" { value = aws_secretsmanager_secret.console_db_password.arn }
