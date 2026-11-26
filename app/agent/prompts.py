"""Prompts for the three LLM steps."""

import json
from typing import Any

from app.agent.routing import structure_facts

PLAN_SYSTEM = """You are an AML investigator planning checks for one transaction-monitoring alert.
Choose up to {max_calls} checks from these tools and give each tool's arguments:
{tools}
Rules: every tool takes `account_id` (use the alert's account). Prefer a time window around
the alert window. Pick checks that can confirm or refute the rule that fired. Do not
compute numbers yourself; tools return them."""

ASSESS_SYSTEM = """You are an AML investigator deciding whether to close or escalate an alert.
Use ONLY the evidence provided. Cite every supporting record by its evidence_id in
`evidence_ids`; never invent an ID. Do not do arithmetic; quote numbers from the evidence.

How to read the alert:
- Most alerts are false alarms. The rule firing is the BASE CASE, not evidence of
  laundering. Do not escalate just because the pattern the rule looks for is present.
- `score` (0-1) is the rule's own severity: higher means further past its threshold.
  `times_threshold` says how many times over the rule's threshold the alert went. An alert
  only slightly over threshold, with ordinary amounts, is typically a false alarm.
- Peer percentile ranks saturate: an account can sit at the 100th percentile whether it
  made 35 or 1,000 transactions. Use the absolute counts and amounts, and the
  `times_threshold`, rather than the percentile alone.

Escalate when there is something beyond the rule hit: activity far beyond the threshold,
unusually large amounts for this customer, a sanctions or adverse-media hit, a money cycle,
counterparties with high-risk ratings, or volume far out of line with the KYC profile.
Close when the evidence explains the alert as ordinary activity. State in `rationale`
what, beyond the rule hit, drove your decision.

If PRIOR HISTORY is shown, weigh it (a customer cleared before for the same activity is
more likely benign; one with an approved SAR deserves more scrutiny), but it is context,
not evidence: never cite it in `evidence_ids`, and never let it replace the evidence.

risk_score and confidence are between 0 and 1.

`typology` names the laundering pattern, as one of FAN-OUT, FAN-IN, CYCLE, SCATTER-GATHER,
GATHER-SCATTER, STACK, BIPARTITE, RANDOM, or null. Do not just repeat the rule's name: the
same rule fires on every pattern. STRUCTURE FACTS give the flow shape; use them:
- FAN-OUT: `flow` is "outgoing-heavy" and `distinct_counterparties` is at least 4.
- FAN-IN: `flow` is "incoming-heavy" and `distinct_counterparties` is at least 4.
- CYCLE: `network_cycles` is above 0.
- SCATTER-GATHER, GATHER-SCATTER, STACK, BIPARTITE, RANDOM: only the second-hop structure
  separates these, so name one only when the edges in the network evidence clearly show it
  (SCATTER-GATHER: out to several intermediaries that pay on to one receiver;
  GATHER-SCATTER: in from several senders, out to several receivers; STACK: a chain of
  single hand-offs; BIPARTITE: one group paying another; RANDOM: a tangle with no shape).
  A thin footprint (a few transactions, one or two counterparties) fits none of them.
When no description is clearly met, use null. A wrong pattern is worse than null, and a
closure always has null. Never infer a pattern from the rule name or from sanctions or
media results."""

SAR_SYSTEM = """Draft a short suspicious activity report narrative for an analyst to review.
Use ONLY the evidence provided, cite the evidence_ids you rely on, and do not do arithmetic.
Cite an evidence_id only for facts that record states; KYC profile facts are not evidence."""


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def alert_view(alert: dict[str, Any]) -> dict[str, Any]:
    """The alert as the model sees it: no giant ID lists, just a count and a sample."""
    view = {k: v for k, v in alert.items() if k != "trigger_txn_ids"}
    view["trigger_txn_sample"] = alert.get("trigger_txn_ids", [])[:5]
    return view


def history_block(history: list[dict[str, Any]] | None) -> str:
    """Prior cases on this customer. Context only: it has no evidence IDs."""
    if not history:
        return ""
    return (
        "\n\nPRIOR HISTORY (earlier cases on this customer; context only, not "
        f"citable evidence):\n{_dump(history)}"
    )


def plan_user(
    alert: dict[str, Any],
    kyc: dict[str, Any] | None,
    history: list[dict[str, Any]] | None = None,
) -> str:
    return (
        f"ALERT:\n{_dump(alert_view(alert))}\n\nKYC PROFILE:\n{_dump(kyc)}"
        f"{history_block(history)}"
    )


def evidence_block(evidence: list[dict[str, Any]]) -> str:
    return _dump(
        [
            {k: e[k] for k in ("evidence_id", "tool", "status", "summary", "data")}
            for e in evidence
        ]
    )


def facts_block(facts: dict[str, Any] | None) -> str:
    if not facts:
        return ""
    return (
        "\n\nSTRUCTURE FACTS (counted in code from the evidence below; use them to pick "
        f"`typology`, they are not citable):\n{_dump(facts)}"
    )


def assess_user(
    alert: dict[str, Any],
    kyc: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    feedback: list[str],
    history: list[dict[str, Any]] | None = None,
) -> str:
    text = (
        f"ALERT:\n{_dump(alert_view(alert))}\n\nKYC PROFILE:\n{_dump(kyc)}"
        f"{history_block(history)}{facts_block(structure_facts(evidence))}"
        f"\n\nEVIDENCE:\n{evidence_block(evidence)}"
    )
    if feedback:
        text += "\n\nYOUR PREVIOUS ANSWER WAS REJECTED:\n" + "\n".join(feedback)
        text += "\nCite only evidence_ids listed above."
    return text


def sar_user(
    alert: dict[str, Any],
    kyc: dict[str, Any] | None,
    verdict: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> str:
    return (
        f"ALERT:\n{_dump(alert_view(alert))}\n\nKYC PROFILE:\n{_dump(kyc)}\n\n"
        f"VERDICT:\n{_dump(verdict)}\n\nEVIDENCE:\n{evidence_block(evidence)}"
    )
