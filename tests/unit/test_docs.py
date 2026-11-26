"""Docs checks (F18): relative links resolve, and README headline numbers match the committed eval run."""

import re
from pathlib import Path

import pytest

from app.eval.metrics import compute_metrics
from app.eval.report import pct
from app.eval.store import read_run_file

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "docs" / "results" / "eval-full-100-v1.json"
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path) -> None:
    text = re.sub(r"```.*?```", "", doc.read_text(), flags=re.DOTALL)
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = (doc.parent / target.split("#")[0]).resolve()
        assert path.exists(), f"{doc.relative_to(ROOT)}: broken link {target}"


def test_readme_headline_matches_eval_run() -> None:
    _, rows = read_run_file(RUN)
    m = compute_metrics(rows)
    readme = (ROOT / "README.md").read_text()
    assert m.overall.recall is not None and m.overall.precision is not None
    assert m.cost.mean is not None and m.seconds.mean is not None
    expected = [
        pct(m.overall.recall),
        pct(m.overall.precision),
        pct(m.grounding_rate),
        f"{m.overall.escalated} ",
        f"${m.cost.mean:.4f}",
        f"{m.seconds.mean:.1f} s",
    ]
    for needle in expected:
        assert needle in readme, f"README does not mention {needle!r}"
    assert m.overall.true_alerts == 50 and m.overall.false_alerts == 50
