# Cost guard. Separate root so `terraform destroy` of the main stack never removes it.
# Apply this first:  terraform -chdir=infra/budget apply -var email=you@example.com

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "email" {
  description = "Where budget alerts go."
  type        = string
}

variable "monthly_limit_usd" {
  type    = number
  default = 50
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "aml-agent"
      Env     = "budget"
    }
  }
}

resource "aws_budgets_budget" "monthly" {
  name         = "aml-agent-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.email]
  }
}
