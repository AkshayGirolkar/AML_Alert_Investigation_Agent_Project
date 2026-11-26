#!/usr/bin/env bash
# Seed the AWS database from the local one (F17).
#
#   scripts/aws_seed.sh dump      # local compose Postgres -> data/aws_seed.dump -> S3
#   scripts/aws_seed.sh restore   # one-off ECS task restores it into RDS
#
# Env: BUCKET (terraform output data_bucket), CLUSTER (cluster_name), AWS_REGION.
# What is dumped is whatever the local DB holds: ~100k transactions if you loaded
# with `scripts.load_hi_small --slice`, the full 5M (about 1.4 GB) otherwise.
# Only reference/data tables go; cases, checkpoints, memory and LLM usage start empty on AWS.
# Run the migrate task first (scripts/aws_deploy.sh) so the schema exists.
set -euo pipefail

TABLES=(accounts adverse_media alerts banks entities kyc_profiles
  laundering_attempt_transactions laundering_attempts planted_matches
  sanctions_entities transactions)
KEY="${SEED_KEY:-seed/aws_seed.dump}"
LOCAL_DUMP="${LOCAL_DUMP:-data/aws_seed.dump}"

case "${1:-}" in
dump)
  : "${BUCKET:?set BUCKET}"
  args=()
  for t in "${TABLES[@]}"; do args+=(-t "public.$t"); done
  mkdir -p "$(dirname "$LOCAL_DUMP")"
  # pg_dump runs inside the compose container so no local PG client is needed (and the
  # version matches the server).
  docker exec aml-postgres pg_dump -U "${POSTGRES_USER:-aml}" -d "${POSTGRES_DB:-aml}" \
    -Fc --data-only "${args[@]}" >"$LOCAL_DUMP"
  ls -lh "$LOCAL_DUMP"
  aws s3 cp "$LOCAL_DUMP" "s3://$BUCKET/$KEY"
  ;;
restore)
  : "${BUCKET:?set BUCKET}" "${CLUSTER:?set CLUSTER}" "${AWS_REGION:?set AWS_REGION}"
  export AWS_DEFAULT_REGION="$AWS_REGION"
  NET=$(aws ecs describe-services --cluster "$CLUSTER" --services api \
    --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)
  OVERRIDES=$(jq -nc --arg b "$BUCKET" --arg k "$KEY" \
    '{containerOverrides:[{name:"migrate",command:["python","-m","scripts.restore_seed","--bucket",$b,"--key",$k]}]}')
  TASK_ARN=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$CLUSTER-migrate" \
    --network-configuration "{\"awsvpcConfiguration\": $(echo "$NET" | jq -c '. + {assignPublicIp:"DISABLED"}')}" \
    --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
  echo "Restore task: $TASK_ARN (logs: /ecs/$CLUSTER/migrate)"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
  CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].containers[0].exitCode' --output text)
  echo "Exit code: $CODE"
  [ "$CODE" = "0" ]
  ;;
*)
  echo "usage: $0 dump|restore" >&2
  exit 2
  ;;
esac
