"""Optional LangSmith sync for eval sets and runs (F15). Local results stay the source of
truth; every failure here is logged and swallowed. Inputs are masked first (F13)."""

import logging
from typing import Any

from app.eval.dataset import EvalDataset
from app.eval.metrics import EvalMetrics, ResultRow
from app.eval.store import RunInfo
from app.observability.pii import mask

log = logging.getLogger("eval.langsmith")


def sync_run(
    client: Any,
    dataset: EvalDataset,
    info: RunInfo,
    rows: list[ResultRow],
    metrics: EvalMetrics,
) -> bool:
    """Upload the dataset once and the run's per-alert results as a project of runs."""
    try:
        name = f"aml-eval-{dataset.name}"
        if not client.has_dataset(dataset_name=name):
            ds = client.create_dataset(
                name, description=f"F15 eval set, seed {dataset.seed}"
            )
            client.create_examples(
                dataset_id=ds.id,
                examples=[
                    {
                        "inputs": {"alert_id": c.alert_id, "rule_id": c.rule_id},
                        "outputs": {
                            "is_true_positive": c.truth,
                            "typology": c.truth_typology,
                        },
                    }
                    for c in dataset.items
                ],
            )
        o = metrics.overall
        summary = {
            "recall": o.recall,
            "precision": o.precision,
            "typology_accuracy": o.typology_accuracy,
            "grounding_rate": metrics.grounding_rate,
            "mean_cost_usd": metrics.cost.mean,
        }
        for r in rows:
            client.create_run(
                name=f"alert-{r.alert_id}",
                run_type="chain",
                project_name=f"aml-eval-{info.run_id}",
                inputs=mask({"alert_id": r.alert_id, "rule_id": r.rule_id}),
                outputs=mask(
                    {
                        "outcome": r.outcome,
                        "typology": r.typology,
                        "risk": r.risk,
                        "truth": r.truth,
                        "error": r.error,
                    }
                ),
                extra={"metadata": {**info.config, **summary, "run_id": info.run_id}},
            )
        return True
    except Exception:
        log.exception("LangSmith sync failed; local results are unaffected")
        return False
