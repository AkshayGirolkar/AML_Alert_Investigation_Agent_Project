# infra/ (F17)

Terraform for the AWS deployment. Spec: `docs/features/F17-aws-terraform.md`.

- `budget/`: monthly cost alarm. Separate root, apply it first, never destroy it with the stack.
- `./` (this folder): network, RDS, ElastiCache, SQS, ECR, Secrets Manager, S3. ECS, ALB, alarms and autoscaling arrive in the next PR.

```bash
terraform -chdir=infra/budget init && terraform -chdir=infra/budget apply -var email=you@example.com

cp infra/envs/dev.tfvars infra/envs/dev.local.tfvars   # set allowed_cidrs to your IP/32
terraform -chdir=infra init
terraform -chdir=infra plan  -var-file=envs/dev.local.tfvars
terraform -chdir=infra apply -var-file=envs/dev.local.tfvars     # you run apply/destroy, not the agent
```

After the first apply, store the API keys (they are deliberately not in state):

```bash
aws secretsmanager put-secret-value --secret-id aml-agent-dev/openai-api-key --secret-string "$OPENAI_API_KEY"
aws secretsmanager put-secret-value --secret-id aml-agent-dev/langsmith-api-key --secret-string "$LANGSMITH_API_KEY"
```

Local state is used for now (`terraform.tfstate`, gitignored). `terraform destroy` empties the S3 bucket and ECR repo (`destroyable = true`).

## Compute, deploy (PR 3)

`terraform apply` also creates the ALB, ECS cluster and services (at **0 tasks** until an image exists), alarms and autoscaling. Set `alert_email` for alarm mail (confirm the SNS subscription) and `github_repo = "owner/name"` to create the deploy role.

First deploy, after `apply`:

```bash
# 1. fill the two API-key secrets (above), 2. build and push an image, 3. roll it out:
export CLUSTER=$(terraform -chdir=infra output -raw cluster_name) AWS_REGION=us-east-1
export IMAGE=$(terraform -chdir=infra output -raw ecr_repository_url):$(git rev-parse --short HEAD)
docker build --platform linux/amd64 -t $IMAGE . && docker push $IMAGE
scripts/aws_deploy.sh
curl http://$(terraform -chdir=infra output -raw alb_dns_name)/ready     # from an allowed IP
```

Later deploys run from GitHub (`.github/workflows/deploy.yml`, repo variables `AWS_ROLE_ARN`, `AWS_REGION`, `ECS_CLUSTER`, `ECR_REPOSITORY`). The ALB only admits `allowed_cidrs`, so the workflow waits for ECS `services-stable` instead of curling it.
