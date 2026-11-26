# Terraform owns the queues; the app only looks them up on real AWS (ensure_queues).
# Attributes mirror app/config.py: visibility timeout > one investigation, redrive after
# SQS_MAX_RECEIVE_COUNT receives.

resource "aws_sqs_queue" "dlq" {
  name                      = var.sqs_dlq_name
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "main" {
  name                       = var.sqs_queue_name
  visibility_timeout_seconds = var.sqs_visibility_timeout
  message_retention_seconds  = 345600 # 4 days
  receive_wait_time_seconds  = 20
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.sqs_max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main.arn]
  })
}
