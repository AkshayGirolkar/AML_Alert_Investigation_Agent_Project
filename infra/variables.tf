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
