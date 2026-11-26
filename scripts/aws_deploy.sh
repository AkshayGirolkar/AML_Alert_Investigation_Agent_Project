#!/usr/bin/env bash
# Roll a new image out to ECS (F17): register new task definition revisions for api,
# worker and migrate, run the migration task, then update the two services.
#
#   CLUSTER=aml-agent-dev IMAGE=<account>.dkr.ecr.us-east-1.amazonaws.com/aml-agent-dev:<sha> \
#     AWS_REGION=us-east-1 scripts/aws_deploy.sh
#
# Needs: aws CLI, jq, and AWS credentials allowed to do what the github_deploy role may.
# Terraform owns the infrastructure; this script owns which image runs (the services
# ignore task_definition changes).
set -euo pipefail

: "${CLUSTER:?set CLUSTER}" "${IMAGE:?set IMAGE}" "${AWS_REGION:?set AWS_REGION}"
export AWS_DEFAULT_REGION="$AWS_REGION"
FAMILY_PREFIX="${FAMILY_PREFIX:-$CLUSTER}"

register() { # family -> new task definition ARN
  aws ecs describe-task-definition --task-definition "$1" --query taskDefinition --output json |
    jq --arg img "$IMAGE" '
      .containerDefinitions[0].image = $img
      | {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
         volumes, requiresCompatibilities, cpu, memory}' >"/tmp/$1.json"
  aws ecs register-task-definition --cli-input-json "file:///tmp/$1.json" \
    --query taskDefinition.taskDefinitionArn --output text
}

echo "Registering task definitions for $IMAGE"
API_TD=$(register "$FAMILY_PREFIX-api")
WORKER_TD=$(register "$FAMILY_PREFIX-worker")
MIGRATE_TD=$(register "$FAMILY_PREFIX-migrate")

# Reuse the api service's subnets and security group for the one-off migration task.
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services api \
  --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)

echo "Running migrations ($MIGRATE_TD)"
TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$MIGRATE_TD" \
  --network-configuration "{\"awsvpcConfiguration\": $(echo "$NET" | jq -c '. + {assignPublicIp:"DISABLED"}')}" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)
if [ "$EXIT_CODE" != "0" ]; then
  echo "Migration failed (exit code $EXIT_CODE); services not updated. Logs: /ecs/$CLUSTER/migrate" >&2
  exit 1
fi

echo "Updating services"
# First deploy: Terraform creates the services at 0 tasks (no image existed yet), so
# start them at their minimum. Afterwards autoscaling owns the desired count.
update() { # service task-definition default-count
  local current args=(--cluster "$CLUSTER" --service "$1" --task-definition "$2")
  current=$(aws ecs describe-services --cluster "$CLUSTER" --services "$1" \
    --query 'services[0].desiredCount' --output text)
  if [ "$current" = "0" ]; then args+=(--desired-count "$3"); fi
  aws ecs update-service "${args[@]}" >/dev/null
}
update api "$API_TD" "${API_COUNT:-2}"
update worker "$WORKER_TD" "${WORKER_COUNT:-1}"
aws ecs wait services-stable --cluster "$CLUSTER" --services api worker
echo "Deployed $IMAGE"
