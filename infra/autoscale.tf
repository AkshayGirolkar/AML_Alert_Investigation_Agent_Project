# Workers scale on SQS backlog per running task; the API on CPU.
# Capacity reference (F16, fake LLM): one worker task ~310 cases/min at concurrency 32;
# with the real LLM the shared rate limiter (~130 cases/min) is the cap, so more than a
# couple of tasks only helps when the limit is raised.

resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  min_capacity       = var.worker_min_tasks
  max_capacity       = var.worker_max_tasks
}

resource "aws_appautoscaling_policy" "worker_backlog" {
  name               = "${local.name}-worker-backlog"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  resource_id        = aws_appautoscaling_target.worker.resource_id

  target_tracking_scaling_policy_configuration {
    target_value = var.worker_backlog_per_task
    # Out fast; in slowly so a scale-in does not SIGTERM tasks mid-investigation
    # (the visibility timeout covers it, but waiting avoids the 5 minute recovery).
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    customized_metric_specification {
      metrics {
        id          = "backlog_per_task"
        expression  = "visible / running"
        label       = "Visible messages per running worker task"
        return_data = true
      }

      metrics {
        id = "visible"
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"
            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.main.name
            }
          }
        }
        return_data = false
      }

      metrics {
        id = "running"
        metric_stat {
          stat = "Average"
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.main.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.worker.name
            }
          }
        }
        return_data = false
      }
    }
  }
}

resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  min_capacity       = var.api_min_tasks
  max_capacity       = var.api_max_tasks
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  resource_id        = aws_appautoscaling_target.api.resource_id

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_out_cooldown = 60
    scale_in_cooldown  = 300

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}
