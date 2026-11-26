# Secret containers. The two API keys are created empty and filled by hand once:
#   aws secretsmanager put-secret-value --secret-id <name> --secret-string '<key>'
# so the values never touch Terraform state. The DB master password is managed by RDS
# (see data.tf).

resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "${local.name}/openai-api-key"
  recovery_window_in_days = var.destroyable ? 0 : 7
}

resource "aws_secretsmanager_secret" "langsmith_api_key" {
  name                    = "${local.name}/langsmith-api-key"
  recovery_window_in_days = var.destroyable ? 0 : 7
}

# Password of the restricted `console_app` DB role (migration 0006 reads CONSOLE_DB_PASSWORD).
# Generated here, so it is in (local, gitignored) state; it only guards a role with
# access to the console schema views.
resource "random_password" "console_db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "console_db_password" {
  name                    = "${local.name}/console-db-password"
  recovery_window_in_days = var.destroyable ? 0 : 7
}

resource "aws_secretsmanager_secret_version" "console_db_password" {
  secret_id     = aws_secretsmanager_secret.console_db_password.id
  secret_string = random_password.console_db.result
}
