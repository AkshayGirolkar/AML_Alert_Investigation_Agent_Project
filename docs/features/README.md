# Feature index

Specs are `Fxx-*.md`; most features also have a plain-language `Fxx-explained.txt`. Build order is in `AML_Alert_Investigation_Agent_Project.md`.

| Feature | What | Spec | Status |
|---|---|---|---|
| F00 | Project scaffold (FastAPI, config, Compose, CI tooling) | none written | Built |
| F01 | Database layer (SQLAlchemy, Alembic, partitioned transactions) | none written | Built |
| F02 | Transaction ingestion (`COPY`, patterns file) | [F02](F02-ingestion.md) | Built |
| F03 | Synthetic KYC, sanctions, adverse media | [F03](F03-synthetic-data.md) | Built |
| F04 | SQL rules engine and rules-only baseline | [F04](F04-rules-engine.md) | Built |
| F05 | Agent tools | [F05](F05-agent-tools.md) | Built |
| F06 | LangGraph investigation graph | [F06](F06-agent-graph.md) | Built |
| F07 | Async execution, SQS, checkpoint resume | [F07](F07-async-execution.md) | Built |
| F08 | Human-in-the-loop review | [F08](F08-human-in-the-loop.md) | Built |
| F09 | Long-term memory | [F09](F09-long-term-memory.md) | Built |
| F10 | Redis (cache, limiter, locks, SSE) | [F10](F10-redis.md) | Built |
| F10.5 | Console contract (views, request table) | [F10.5](F10_5-console-contract.md) | Built |
| F11 | Auth and RBAC | [F11](F11-auth-rbac.md) | Built; Cognito optional in Terraform |
| F12 | LLM gateway | [F12](F12-llm-gateway.md) | Built |
| F13 | Observability | [F13](F13-observability.md) | Built |
| F14 | Analyst console (separate repo) | [F14](F14-analyst-console.md) | Spec only, not built |
| F15 | Evaluation harness | [F15](F15-eval-harness.md) | Built |
| F16 | Load test | [F16](F16-load-test.md) | Built |
| F17 | AWS with Terraform | [F17](F17-aws-terraform.md) | Code complete, never applied |
| F18 | Portfolio polish | [F18](F18-portfolio-polish.md) | Built; demo recording pending |
| F19 | Typology naming in the verdict | [F19](F19-typology-prompt.md) | Built; typology accuracy 2.6%, target not met |
