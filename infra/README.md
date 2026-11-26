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
