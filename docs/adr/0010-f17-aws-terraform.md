# ADR 0010 – F17 AWS deployment with Terraform

## Status
Accepted. Code complete; the first apply, seed, smoke test and AWS sweep have not been run yet (the results section below is a template to fill in).

## Context
The stack ran only on Docker Compose. F17 deploys the same image to AWS, as code, with a hard cost bound: the environment exists for test and demo sessions and is destroyed afterwards.

## Decisions
1. **Same image, three commands.** One ECR image runs the API (`uvicorn`), the worker (`python -m app.worker`) and a one-shot migrate/seed task (`alembic upgrade head`, or `scripts.restore_seed`). No AWS-only code path in the agent.
2. **Terraform owns infrastructure, a script owns the image.** The ECS services ignore `task_definition` and `desired_count` changes. `scripts/aws_deploy.sh` registers new task definition revisions, runs migrations as a task, then updates the services. This is why CI can deploy with a narrow OIDC role and no Terraform state: the state is local, so CI cannot run `terraform apply`.
3. **Terraform owns the queues.** On real AWS (`AWS_ENDPOINT_URL` unset) the app looks queues up instead of creating them, so the task role has no `sqs:CreateQueue` and cannot drift from the redrive policy. LocalStack still creates them.
4. **Secrets:** the RDS master password is RDS-managed (never in state); OpenAI and LangSmith keys are empty secrets filled by hand; only the generated `console_app` password is in (local, gitignored) state. ECS injects them as env vars. A side effect found while wiring this: RDS-generated passwords can contain URL-reserved characters, so `Settings` now builds DB URLs with SQLAlchemy `URL.create` (escaped).
5. **Network:** public subnets for the ALB and one NAT gateway; tasks, RDS and Redis in private subnets; RDS and Redis security groups admit only the task security group. One NAT keeps dev cheap and is a single point of failure by design. The ALB admits only `allowed_cidrs` (your IP) over plain HTTP, so there is **no authentication in the API yet** (F11 was never built; Cognito is out of scope). Treat the dev stack as private.
6. **Load tests run inside the VPC** as an ECS task, because RDS and Redis are not reachable from a laptop. The ALB also admits the NAT IP for that. The sampler reads CloudWatch (1-minute resolution) instead of `docker stats`.
7. **Autoscaling:** workers on SQS visible messages per running task (target 40, min 1, max 4, scale-in cooldown 300 s), API on CPU. The real-LLM ceiling is the shared rate limiter (about 130 cases/min, F16), so more than a few tasks only helps if the limit is raised.
8. **Cost guard first:** the Budgets alarm lives in its own root (`infra/budget`) so `destroy` cannot remove it. `destroyable = true` (dev) skips the final snapshot and lets destroy empty S3 and ECR.

## Deliberately not done
HTTPS/custom domain, WAF, Multi-AZ RDS, Redis replicas, remote Terraform state, Cognito/JWT, the console deployment, a Bedrock fallback, real-LLM load tests on AWS, blue/green deploys. The deploy workflow waits for ECS `services-stable` and does not curl `/ready` (the ALB blocks GitHub runners).

## Things the spec assumed that turned out to exist already
Graceful SIGTERM handling in the worker (`app/worker/main.py`) and the `vector`/`pg_trgm` extensions (migration 0002), so neither needed new code.

## Cost (estimate from us-east-1 list prices, check before relying on it)
Running 24/7: roughly $0.25-0.30 per hour, about $6-7 per day, $180-200 per month. NAT gateway (about $33/month plus data), RDS db.t4g.medium and ElastiCache cache.t4g.small, the ALB and four Fargate tasks are the bulk. Stopped (`terraform destroy`): storage-only leftovers, near $0; the budget alarm costs nothing. Real OpenAI usage is separate.

## Unverified until the first real run
Autoscaling metric math (`visible / RunningTaskCount`, needs Container Insights), the `ecs:cluster` IAM condition on the deploy role, `run-task` command overrides, CloudWatch sampler output, `pg_restore` of the 1.4 GB dump on the migrate task size (512 CPU, 1 GB; raise it if the task is killed), and Container Insights cost.

## Results (fill in after the AWS session)
| Date | Config | Throughput (cases/min) | p95 e2e | Errors | Duplicates | DLQ | First saturating resource |
|------|--------|------------------------|---------|--------|------------|-----|---------------------------|
|      |        |                        |         |        |            |     |                           |

Compare with the local numbers in ADR 0009 (one laptop: 287 cases/min for a 1,000 burst at concurrency 32). Also record: autoscaling out and in on a 200-alert burst, and the worker-kill drill (expect a 5-minute wait for the visibility timeout).
