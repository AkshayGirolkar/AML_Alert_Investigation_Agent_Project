# Evaluation report: full-100-v2

Dataset `eval-100`, 100 alerts (50 true, 50 false). Planner `gpt-4o-mini`, assess `gpt-4o`, git `3bbd12b`.

## Agent vs rules-only baseline

Rules-only means an analyst works every alert (escalate all), so its recall on alerted launderers is 100% by construction. The question is how much workload the agent removes and how many launderers it lets through.

| | Rules only | Agent |
|---|---|---|
| Recall | 100.0% | 88.0% (76-94%) |
| Precision of escalations | 50.0% | 58.7% (47-69%) |
| Alerts sent to an analyst | 100 | 75 |
| False closures (true alerts missed) | 0 | 6 |
| False alerts closed | 0 | 19 |

Workload reduction 25.0%, recall lost 12.0%.

## Recall by typology

| Typology | True alerts | Recall (95% CI) | False closure | Typology accuracy | Named | Right when named |
|---|---|---|---|---|---|---|
| **All** | 50 | 88.0% (76-94%) | 12.0% | 2.6% | 20.5% | 12.5% |
| BIPARTITE | 6 | 83.3% (44-97%) | 16.7% | 0.0% | 40.0% | 0.0% |
| CYCLE | 6 | 100.0% (61-100%) | 0.0% | 0.0% | 0.0% | n/a |
| FAN-IN | 6 | 83.3% (44-97%) | 16.7% | 0.0% | 20.0% | 0.0% |
| FAN-OUT | 6 | 83.3% (44-97%) | 16.7% | 20.0% | 20.0% | 100.0% |
| GATHER-SCATTER | 6 | 66.7% (30-90%) | 33.3% | 0.0% | 50.0% | 0.0% |
| RANDOM | 5 | 100.0% (57-100%) | 0.0% | 0.0% | 0.0% | n/a |
| SCATTER-GATHER | 5 | 100.0% (57-100%) | 0.0% | 0.0% | 20.0% | 0.0% |
| STACK | 5 | 80.0% (38-96%) | 20.0% | 0.0% | 25.0% | 0.0% |
| UNTYPED | 5 | 100.0% (57-100%) | 0.0% | n/a | n/a | n/a |

Typology accuracy counts escalated true alerts whose named typology matches the IBM patterns file; a null typology counts as wrong. 'Named' is the share where the agent named a pattern at all.

## Typology confusion (escalated true alerts)

| True typology | Named by the agent |
|---|---|
| BIPARTITE | (none) x3, CYCLE x1, FAN-OUT x1 |
| CYCLE | (none) x6 |
| FAN-IN | (none) x4, CYCLE x1 |
| FAN-OUT | (none) x4, FAN-OUT x1 |
| GATHER-SCATTER | (none) x2, FAN-IN x2 |
| RANDOM | (none) x5 |
| SCATTER-GATHER | (none) x4, CYCLE x1 |
| STACK | (none) x3, FAN-OUT x1 |
| UNTYPED | (none) x1, CYCLE x1, FAN-OUT x3 |

## Quality, cost and latency

- Grounding rate: 100.0% of scored verdicts are grounded and cite at least one evidence id
- Error rate: 0.0%
- Mean tool calls: 5.0
- Cost per case: mean $0.0174, p50 $0.0154, p95 $0.0310
- Latency per case: mean 9.9s, p50 9.6s, p95 12.5s
- Mean cost by node: assess $0.0170, plan $0.0004

## Missed true alerts (first 10)

- alert 43267 (fan_out, STACK), risk 0.2, confidence 0.9: The alert was triggered by a fan-out pattern with 20 outgoing transactions to 10 distinct counterparties, which is 2.5 times over the threshold. However, the total outgoing amount of 76,290.67 Euros i
- alert 43757 (fan_out, BIPARTITE), risk 0.1, confidence 0.9: The alert is based on a fan-out pattern with 22 outgoing transactions to 10 distinct counterparties, which is slightly above the threshold. The total outgoing amount of 196,337.75 Euro is well within 
- alert 53370 (round_trip, FAN-IN), risk 0.1, confidence 0.9: The alert is based on a rule hit for round-trip transactions, but the evidence shows only two incoming transactions from a single counterparty, with no outgoing transactions or network cycles. The amo
- alert 53379 (round_trip, GATHER-SCATTER), risk 0.1, confidence 0.9: The alert is based on a rule hit for round-trip transactions, but the activity is not suspicious. The account had 5 transactions with 2 counterparties, which is not indicative of complex laundering pa
- alert 53645 (round_trip, FAN-OUT), risk 0.2, confidence 0.9: The alert is a false alarm. The transaction volume is within the expected monthly volume for the account, and there are no sanctions or adverse media hits. The account's activity is not significantly 
- alert 53655 (round_trip, GATHER-SCATTER), risk 0.2, confidence 0.9: The alert is based on a rule hit for round-trip transactions, but the activity is within the expected monthly volume for the account and does not show any suspicious patterns. The total outgoing amoun
