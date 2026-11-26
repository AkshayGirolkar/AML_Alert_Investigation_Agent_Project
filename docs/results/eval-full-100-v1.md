# Evaluation report: full-100-v1

Dataset `eval-100`, 100 alerts (50 true, 50 false). Planner `gpt-4o-mini`, assess `gpt-4o`, git `72a99d2`.

## Agent vs rules-only baseline

Rules-only means an analyst works every alert (escalate all), so its recall on alerted launderers is 100% by construction. The question is how much workload the agent removes and how many launderers it lets through.

| | Rules only | Agent |
|---|---|---|
| Recall | 100.0% | 90.0% (79-96%) |
| Precision of escalations | 50.0% | 55.6% (45-66%) |
| Alerts sent to an analyst | 100 | 81 |
| False closures (true alerts missed) | 0 | 5 |
| False alerts closed | 0 | 14 |

Workload reduction 19.0%, recall lost 10.0%.

## Recall by typology

| Typology | True alerts | Recall (95% CI) | False closure | Typology accuracy |
|---|---|---|---|---|
| **All** | 50 | 90.0% (79-96%) | 10.0% | 0.0% |
| BIPARTITE | 6 | 83.3% (44-97%) | 16.7% | 0.0% |
| CYCLE | 6 | 100.0% (61-100%) | 0.0% | 0.0% |
| FAN-IN | 6 | 83.3% (44-97%) | 16.7% | 0.0% |
| FAN-OUT | 6 | 100.0% (61-100%) | 0.0% | 0.0% |
| GATHER-SCATTER | 6 | 66.7% (30-90%) | 33.3% | 0.0% |
| RANDOM | 5 | 100.0% (57-100%) | 0.0% | 0.0% |
| SCATTER-GATHER | 5 | 100.0% (57-100%) | 0.0% | 0.0% |
| STACK | 5 | 80.0% (38-96%) | 20.0% | 0.0% |

Typology accuracy counts escalated true alerts whose named typology matches the IBM patterns file.

## Quality, cost and latency

- Grounding rate: 100.0% of scored verdicts are grounded and cite at least one evidence id
- Error rate: 0.0%
- Mean tool calls: 5.0
- Cost per case: mean $0.0175, p50 $0.0155, p95 $0.0323
- Latency per case: mean 11.5s, p50 11.7s, p95 16.0s
- Mean cost by node: assess $0.0171, plan $0.0004

## Missed true alerts (first 10)

- alert 43267 (fan_out, STACK), risk 0.2, confidence 0.9: The alert was triggered by a fan-out pattern with 20 outgoing transactions to 10 distinct counterparties, which is 2.5 times over the threshold. However, the total outgoing amount of 76,290.67 Euros i
- alert 43757 (fan_out, BIPARTITE), risk 0.2, confidence 0.9: The alert was triggered by a fan-out pattern with 10 distinct counterparties, slightly above the threshold of 8. However, the total outgoing amount of 196,337.75 Euro is well within the expected month
- alert 53370 (round_trip, FAN-IN), risk 0.1, confidence 0.9: The alert is based on a rule hit for round-trip transactions, but the evidence does not support escalation. The account had only 2 incoming transactions from a single counterparty, with no outgoing tr
- alert 53379 (round_trip, GATHER-SCATTER), risk 0.2, confidence 0.9: The alert is based on a round-trip pattern, but the transactions involved are within the expected activity for the account. The total outgoing amount is only 0.015 of the expected monthly volume, indi
- alert 53655 (round_trip, GATHER-SCATTER), risk 0.2, confidence 0.9: The alert is based on a round-trip pattern, but the transactions are within the expected monthly volume for the account and do not indicate any suspicious activity. The total outgoing transactions amo
