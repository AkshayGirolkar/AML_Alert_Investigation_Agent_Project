# Evaluation report: tune100-new-prompt-v2

Dataset `tune-100`, 100 alerts (50 true, 50 false). Planner `gpt-4o-mini`, assess `gpt-4o`, git `3bbd12b`.

## Agent vs rules-only baseline

Rules-only means an analyst works every alert (escalate all), so its recall on alerted launderers is 100% by construction. The question is how much workload the agent removes and how many launderers it lets through.

| | Rules only | Agent |
|---|---|---|
| Recall | 100.0% | 92.0% (81-97%) |
| Precision of escalations | 50.0% | 57.5% (47-68%) |
| Alerts sent to an analyst | 100 | 80 |
| False closures (true alerts missed) | 0 | 4 |
| False alerts closed | 0 | 16 |

Workload reduction 20.0%, recall lost 8.0%.

## Recall by typology

| Typology | True alerts | Recall (95% CI) | False closure | Typology accuracy | Named | Right when named |
|---|---|---|---|---|---|---|
| **All** | 50 | 92.0% (81-97%) | 8.0% | 7.3% | 34.1% | 21.4% |
| BIPARTITE | 6 | 100.0% (61-100%) | 0.0% | 0.0% | 50.0% | 0.0% |
| CYCLE | 6 | 83.3% (44-97%) | 16.7% | 0.0% | 40.0% | 0.0% |
| FAN-IN | 6 | 100.0% (61-100%) | 0.0% | 16.7% | 50.0% | 33.3% |
| FAN-OUT | 6 | 83.3% (44-97%) | 16.7% | 40.0% | 40.0% | 100.0% |
| GATHER-SCATTER | 6 | 100.0% (61-100%) | 0.0% | 0.0% | 33.3% | 0.0% |
| RANDOM | 5 | 80.0% (38-96%) | 20.0% | 0.0% | 0.0% | n/a |
| SCATTER-GATHER | 5 | 100.0% (57-100%) | 0.0% | 0.0% | 20.0% | 0.0% |
| STACK | 5 | 80.0% (38-96%) | 20.0% | 0.0% | 25.0% | 0.0% |
| UNTYPED | 5 | 100.0% (57-100%) | 0.0% | n/a | n/a | n/a |

Typology accuracy counts escalated true alerts whose named typology matches the IBM patterns file; a null typology counts as wrong. 'Named' is the share where the agent named a pattern at all.

## Typology confusion (escalated true alerts)

| True typology | Named by the agent |
|---|---|
| BIPARTITE | (none) x3, CYCLE x1, FAN-IN x1, FAN-OUT x1 |
| CYCLE | (none) x3, FAN-IN x2 |
| FAN-IN | (none) x3, CYCLE x2, FAN-IN x1 |
| FAN-OUT | (none) x3, FAN-OUT x2 |
| GATHER-SCATTER | (none) x4, FAN-OUT x2 |
| RANDOM | (none) x4 |
| SCATTER-GATHER | (none) x4, FAN-IN x1 |
| STACK | (none) x3, FAN-IN x1 |
| UNTYPED | (none) x2, FAN-OUT x3 |

## Quality, cost and latency

- Grounding rate: 100.0% of scored verdicts are grounded and cite at least one evidence id
- Error rate: 0.0%
- Mean tool calls: 5.0
- Cost per case: mean $0.0189, p50 $0.0182, p95 $0.0326
- Latency per case: mean 10.8s, p50 10.8s, p95 14.5s
- Mean cost by node: assess $0.0185, plan $0.0004

## Missed true alerts (first 10)

- alert 44674 (fan_out, FAN-OUT), risk 0.1, confidence 0.9: The alert is based on a fan-out pattern with 10 distinct counterparties, slightly above the threshold of 8. The account's activity is consistent with its KYC profile as an e-commerce business, and the
- alert 47441 (fan_out, STACK), risk 0.2, confidence 0.9: The alert was triggered by a fan-out pattern with 16 outgoing transactions to 10 distinct counterparties, slightly above the threshold. The total outgoing amount is well within the expected monthly vo
- alert 53389 (round_trip, RANDOM), risk 0.2, confidence 0.9: The alert is based on a round-trip pattern, but the transaction amounts are not significant compared to the expected monthly volume of the account. The total outgoing transactions amount to 82,730.99 
- alert 53648 (round_trip, CYCLE), risk 0.2, confidence 0.9: The alert is based on a rule hit for round-trip transactions, but the activity is within the expected range for the account. The total outgoing transactions amount to 99,895.95 Saudi Riyal, which is o
