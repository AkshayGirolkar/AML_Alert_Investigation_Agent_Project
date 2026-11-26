# Demo: one alert from investigation to analyst approval

About five minutes, local stack, **no OpenAI spend**: the worker runs on the deterministic fake LLM (`LLM_PROVIDER=fake`, F16). Everything else is real: API, SQS (LocalStack), worker, LangGraph graph with its tools, Postgres, Redis, checkpoints, memory and audit log. For a real-model demo leave `LLM_PROVIDER=openai` and set `OPENAI_API_KEY`; the commands are the same and a case costs about $0.02.

Verified on 2026-11-26 against the dev database (181,699 alerts loaded).

## 0. Start
```bash
docker compose up -d --build postgres redis localstack migrate api
LLM_PROVIDER=fake FAKE_LLM_ESCALATE_PCT=100 FAKE_LLM_LATENCY_MS=300 docker compose up -d worker
curl localhost:8000/health        # {"status":"ok"}
curl localhost:8000/ready         # postgres, redis, sqs status
```
Open `http://localhost:8000/docs` (Swagger UI) to follow along. With `AUTH_MODE=off` (default) no token is needed; with `AUTH_MODE=jwt` add `-H "Authorization: Bearer $(python -m scripts.mint_token --user ana --role analyst)"` to each call.

## 1. Pick an alert and start the investigation
```bash
docker compose exec -T postgres psql -U aml -d aml -Atc \
  "select a.alert_id, a.rule_id from alerts a left join cases c on c.alert_id=a.alert_id
   where c.alert_id is null order by random() limit 1"
ALERT=<id from above>
curl -s -XPOST localhost:8000/alerts/$ALERT/investigations
```
Returns `202` with `"status":"queued"` and a `case_id`. Repeating the call is safe: the case is created once.

## 2. Watch it run
```bash
CASE=<case_id>
curl -N localhost:8000/cases/$CASE/events      # Server-Sent Events, Ctrl-C to stop
curl -s localhost:8000/alerts/$ALERT/case      # status, verdict, SAR draft
```
Within seconds the case is `awaiting_review`. The verdict lists `evidence_ids` (each one exists in graph state; the grounding check rejects any that don't) and the `sar_draft` narrative cites them. The graph is parked at `human_review` in a Postgres checkpoint: stop the worker now and nothing is lost.

## 3. Analyst decision
```bash
curl -s -XPOST localhost:8000/cases/$CASE/decision -H 'content-type: application/json' \
  -d '{"decision":"approve","analyst_id":"ana","notes":"demo"}'
```
The API records the decision and an audit-log row, and sends a resume message; the worker resumes the graph from its checkpoint, writes the outcome to long-term memory and completes the case.
```bash
curl -s localhost:8000/cases/$CASE                 # status "completed", outcome "escalated"
docker compose exec -T postgres psql -U aml -d aml -Atc \
  "select decision, analyst_id from decisions where case_id=$CASE"
```
Investigating a second alert on the same customer now shows the earlier outcome in the agent's history context, and a prior approved escalation blocks auto-close.

## 4. Show the measured side (no cost)
```bash
python -m scripts.eval_gate --mode replay     # re-scores the recorded 100-alert run against thresholds
```
Results: `docs/results/eval-full-100-v1.md`; load test: `docs/results/loadtest-full-1000.md`.

## 5. Put the worker back
```bash
docker compose up -d worker                    # back to the .env LLM provider
```

## Recording
Not yet recorded. Suggested shot list for a 3-4 minute screen capture: README headline chart (20 s), steps 1-3 in a terminal beside Swagger UI (2 min), the grounding rejection and the worker-kill drill from ADR 0009 (1 min), eval and load-test tables (30 s). Link the video from the README once it exists.
