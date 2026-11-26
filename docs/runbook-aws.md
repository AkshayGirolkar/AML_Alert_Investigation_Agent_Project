# AWS runbook (F17)

Everything here is run by you, with your AWS credentials. Commands for each step are in `infra/README.md`.

## Start a session
1. **Once per account:** `infra/budget` apply (set your email), confirm the SNS/Budgets email.
2. `cp infra/envs/dev.tfvars infra/envs/dev.local.tfvars`; set `allowed_cidrs` to your current IP (`curl -s https://checkip.amazonaws.com`), `alert_email`, and `github_repo` if you want CI deploys.
3. `terraform -chdir=infra apply -var-file=envs/dev.local.tfvars` (about 15 minutes; RDS is slowest).
4. Put the two API keys into Secrets Manager (`put-secret-value`).
5. Build and push the image, run `scripts/aws_deploy.sh` (this also runs migrations and starts the services).
6. `scripts/aws_seed.sh dump` then `restore`.
7. `python -m scripts.aws_smoke --api-url http://<alb>`; expect `RESULT: ok`.

## Load test (fake LLM)
Apply with `-var llm_provider=fake`, redeploy, run the sweep as the ECS task shown in `infra/README.md`. Results are uploaded to `s3://<bucket>/loadtest/results/`. Switch back with `-var llm_provider=openai` afterwards.

## Common problems
- **API unreachable:** your IP changed; update `allowed_cidrs` and apply (a security-group change only).
- **Tasks stop at start:** read the logs in `/ecs/aml-agent-dev/<api|worker|migrate>`; usual causes are an empty API-key secret, or the image tag missing in ECR.
- **Cases stuck `running` after a worker restart:** they reappear after the 300 s visibility timeout and resume from the checkpoint; this is expected.
- **Messages in the DLQ:** the `dlq-not-empty` alarm fires. Inspect them in the SQS console; do not purge before looking.
- **Rotate a secret:** `put-secret-value`, then `aws ecs update-service --force-new-deployment` for `api` and `worker`.

## End a session (do this every time)
1. Download anything you want from S3 (`loadtest/results/`), then `terraform -chdir=infra destroy -var-file=envs/dev.local.tfvars`.
2. Check nothing billing is left (console or CLI): NAT gateways, Elastic IPs, load balancers, RDS instances and snapshots, ElastiCache clusters, CloudWatch log groups (retention 14 days), ECR repositories, S3 buckets.
3. Leave `infra/budget` in place.
