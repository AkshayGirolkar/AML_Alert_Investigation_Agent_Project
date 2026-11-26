# AML Alert Investigation Agent

An agent that investigates anti-money-laundering alerts: a deterministic SQL rules engine raises alerts on the IBM synthetic AML dataset, a LangGraph agent investigates each one with grounded evidence, a human approves escalations, and every verdict is scored against the dataset's ground-truth labels and compared with a rules-only baseline.

Design: `AML_Alert_Investigation_Agent_Project.md`. Feature specs: `docs/features/`. Decisions: `docs/adr/`. Working notes for contributors and for Claude Code: `CLAUDE.md`.

## Run locally
```bash
source .venv/bin/activate && uv pip install -r requirements-dev.txt
docker compose up -d --build        # postgres, redis, localstack, migrate, api (:8000), worker
pytest
```

## Deploy to AWS (F17)
Terraform in `infra/` builds the VPC, ECS Fargate services (API, worker), RDS Postgres, ElastiCache Redis, SQS + DLQ, Secrets Manager, CloudWatch alarms and autoscaling. See `infra/README.md` for the commands, `docs/runbook-aws.md` for a session start/stop checklist, `docs/architecture-aws.md` for the diagram and `docs/adr/0010-f17-aws-terraform.md` for the decisions.

**Cost:** about $6-7 per day while it runs (estimate, mostly NAT, RDS, Redis, ALB and Fargate), so run `terraform destroy` after each session. A separate budget alarm (`infra/budget`) stays in place.

**Security note:** the API has no authentication yet; the dev ALB only admits the IP addresses you list.
