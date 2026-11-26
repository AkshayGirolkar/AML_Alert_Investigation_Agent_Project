variable "region" {
  type    = string
  default = "us-east-1"
}

variable "project" {
  type    = string
  default = "aml-agent"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "allowed_cidrs" {
  description = "CIDR blocks allowed to reach the ALB (your IP, e.g. 203.0.113.7/32). No default on purpose."
  type        = list(string)
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 50
}

variable "db_name" {
  type    = string
  default = "aml"
}

variable "db_username" {
  type    = string
  default = "aml"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

# Must match app/config.py (SQS_VISIBILITY_TIMEOUT, SQS_MAX_RECEIVE_COUNT, queue names).
variable "sqs_queue_name" {
  type    = string
  default = "aml-investigations"
}

variable "sqs_dlq_name" {
  type    = string
  default = "aml-investigations-dlq"
}

variable "sqs_visibility_timeout" {
  type    = number
  default = 300
}

variable "sqs_max_receive_count" {
  type    = number
  default = 3
}

variable "destroyable" {
  description = "Dev convenience: skip the final DB snapshot and let destroy empty the bucket and ECR repo."
  type        = bool
  default     = true
}

# --- compute (PR 3) ---------------------------------------------------------

variable "image_tag" {
  description = "Image tag (git sha) in ECR. Empty = nothing deployed yet: services are created with 0 tasks."
  type        = string
  default     = ""
}

variable "api_cpu" {
  type    = number
  default = 512
}

variable "api_memory" {
  type    = number
  default = 1024
}

variable "api_min_tasks" {
  type    = number
  default = 2
}

variable "api_max_tasks" {
  type    = number
  default = 4
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "worker_min_tasks" {
  type    = number
  default = 1
}

variable "worker_max_tasks" {
  type    = number
  default = 4
}

variable "worker_concurrency" {
  type    = number
  default = 8
}

variable "worker_backlog_per_task" {
  description = "Autoscaling target: visible SQS messages per running worker task."
  type        = number
  default     = 40
}

variable "llm_provider" {
  description = "openai (real) or fake (load tests, no key needed)."
  type        = string
  default     = "openai"
}

variable "langsmith_tracing" {
  type    = bool
  default = false
}

variable "alert_email" {
  description = "Email for CloudWatch alarm notifications (confirm the SNS subscription)."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "github_repo" {
  description = "owner/name allowed to deploy via OIDC. Empty = do not create the GitHub OIDC role."
  type        = string
  default     = ""
}

variable "enable_cognito" {
  description = "Create a Cognito user pool and run the API with AUTH_MODE=jwt. Off = no API auth (keep allowed_cidrs tight)."
  type        = bool
  default     = false
}
