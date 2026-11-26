# Cognito user pool for API authentication (F11). Off by default (enable_cognito = false):
# the API then runs AUTH_MODE=off behind the IP-restricted ALB. When on, the api and worker
# tasks get AUTH_MODE=jwt plus the pool's JWKS URL, issuer and client id, and callers send
# a Cognito access token. Users are not created here (their passwords would land in state):
# see "API authentication" in infra/README.md.

locals {
  cognito_issuer = var.enable_cognito ? "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main[0].id}" : ""

  auth_env = var.enable_cognito ? [
    { name = "AUTH_MODE", value = "jwt" },
    { name = "AUTH_ISSUER", value = local.cognito_issuer },
    { name = "AUTH_JWKS_URL", value = "${local.cognito_issuer}/.well-known/jwks.json" },
    { name = "AUTH_AUDIENCE", value = aws_cognito_user_pool_client.api[0].id },
  ] : []
}

resource "aws_cognito_user_pool" "main" {
  count = var.enable_cognito ? 1 : 0

  name                     = local.name
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF"
  deletion_protection      = var.destroyable ? "INACTIVE" : "ACTIVE"

  # Analysts are invited by an administrator; nobody can sign themselves up.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

# Public client (no secret) for the console and CLI. The API reads `cognito:groups` and
# `client_id` from the access token, so access tokens are what callers send.
resource "aws_cognito_user_pool_client" "api" {
  count = var.enable_cognito ? 1 : 0

  name         = "${local.name}-api"
  user_pool_id = aws_cognito_user_pool.main[0].id

  generate_secret = false
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_USER_PASSWORD_AUTH", # lets scripts/aws_cognito_token.sh log in with the AWS CLI
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60 # minutes: the API does not check revocation, so keep short
  id_token_validity      = 60
  refresh_token_validity = 12 # hours
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

# Group names must match the roles the API knows.
resource "aws_cognito_user_group" "role" {
  for_each = var.enable_cognito ? toset(["analyst", "supervisor"]) : toset([])

  name         = each.key
  user_pool_id = aws_cognito_user_pool.main[0].id
  description  = each.key == "supervisor" ? "Can also decide high-risk cases" : "Investigates alerts and decides cases"
}
