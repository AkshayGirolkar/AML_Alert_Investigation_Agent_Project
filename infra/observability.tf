resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alert_email # confirm the subscription from the email AWS sends
}

locals {
  alarms = {
    dlq-not-empty = {
      namespace = "AWS/SQS", metric = "ApproximateNumberOfMessagesVisible", stat = "Maximum"
      dims      = { QueueName = aws_sqs_queue.dlq.name }
      op        = "GreaterThanThreshold", threshold = 0, periods = 1, period = 60
      desc      = "Poison message(s) in the DLQ"
    }
    queue-oldest-message = {
      namespace = "AWS/SQS", metric = "ApproximateAgeOfOldestMessage", stat = "Maximum"
      dims      = { QueueName = aws_sqs_queue.main.name }
      op        = "GreaterThanThreshold", threshold = 900, periods = 2, period = 60
      desc      = "Work has been waiting over 15 minutes (workers down or saturated)"
    }
    alb-5xx = {
      namespace = "AWS/ApplicationELB", metric = "HTTPCode_Target_5XX_Count", stat = "Sum"
      dims      = { LoadBalancer = aws_lb.api.arn_suffix }
      op        = "GreaterThanThreshold", threshold = 5, periods = 2, period = 300
      desc      = "API returning 5xx"
    }
    api-cpu = {
      namespace = "AWS/ECS", metric = "CPUUtilization", stat = "Average"
      dims      = { ClusterName = aws_ecs_cluster.main.name, ServiceName = "api" }
      op        = "GreaterThanThreshold", threshold = 85, periods = 3, period = 60
      desc      = "API tasks CPU high"
    }
    rds-cpu = {
      namespace = "AWS/RDS", metric = "CPUUtilization", stat = "Average"
      dims      = { DBInstanceIdentifier = aws_db_instance.main.identifier }
      op        = "GreaterThanThreshold", threshold = 85, periods = 3, period = 300
      desc      = "RDS CPU high"
    }
    rds-connections = {
      namespace = "AWS/RDS", metric = "DatabaseConnections", stat = "Maximum"
      dims      = { DBInstanceIdentifier = aws_db_instance.main.identifier }
      op        = "GreaterThanThreshold", threshold = 150, periods = 2, period = 300
      desc      = "RDS connections near the limit"
    }
    rds-free-storage = {
      namespace = "AWS/RDS", metric = "FreeStorageSpace", stat = "Minimum"
      dims      = { DBInstanceIdentifier = aws_db_instance.main.identifier }
      op        = "LessThanThreshold", threshold = 5368709120, periods = 1, period = 300
      desc      = "RDS has under 5 GB free"
    }
    redis-cpu = {
      namespace = "AWS/ElastiCache", metric = "EngineCPUUtilization", stat = "Average"
      dims      = { ReplicationGroupId = aws_elasticache_replication_group.main.replication_group_id }
      op        = "GreaterThanThreshold", threshold = 80, periods = 3, period = 300
      desc      = "Redis CPU high"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarms

  alarm_name          = "${local.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.stat
  dimensions          = each.value.dims
  comparison_operator = each.value.op
  threshold           = each.value.threshold
  evaluation_periods  = each.value.periods
  period              = each.value.period
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
