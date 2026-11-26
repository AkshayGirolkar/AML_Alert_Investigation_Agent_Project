"""Minimal Prometheus-text metrics (F13), no extra dependency.

Each process (API, worker) keeps its own registry. Anything that is already durable in
Postgres (cases by status, review backlog, LLM spend) or SQS (queue depth) is read at
scrape time by `/metrics` instead of being double-counted in memory.
Recording is plain dict arithmetic under a lock, so it cannot fail a case.
"""

import threading
from collections import defaultdict

Labels = tuple[tuple[str, str], ...]

DEFAULT_BUCKETS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)


def _labels(d: dict[str, str]) -> Labels:
    return tuple(sorted(d.items()))


def _fmt(name: str, labels: Labels, value: float, extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in labels]
    if extra:
        parts.append(extra)
    body = "{" + ",".join(parts) + "}" if parts else ""
    return f"{name}{body} {value:g}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[Labels, float]] = defaultdict(dict)
        self._hists: dict[str, dict[Labels, tuple[list[int], float, int]]] = (
            defaultdict(dict)
        )
        self._help: dict[str, str] = {}

    def inc(self, name: str, amount: float = 1.0, /, **labels: str) -> None:
        key = _labels(labels)
        with self._lock:
            series = self._counters[name]
            series[key] = series.get(key, 0.0) + amount

    def observe(self, name: str, value: float, /, **labels: str) -> None:
        key = _labels(labels)
        with self._lock:
            counts, total, n = self._hists[name].get(
                key, ([0] * len(DEFAULT_BUCKETS), 0.0, 0)
            )
            counts = [c + (value <= b) for c, b in zip(counts, DEFAULT_BUCKETS)]
            self._hists[name][key] = (counts, total + value, n + 1)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines += [_fmt(name, k, v) for k, v in sorted(series.items())]
            for name, hseries in sorted(self._hists.items()):
                lines.append(f"# TYPE {name} histogram")
                for k, (counts, total, n) in sorted(hseries.items()):
                    for b, c in zip(DEFAULT_BUCKETS, counts):
                        lines.append(_fmt(f"{name}_bucket", k, c, f'le="{b:g}"'))
                    lines.append(_fmt(f"{name}_bucket", k, n, 'le="+Inf"'))
                    lines.append(_fmt(f"{name}_sum", k, total))
                    lines.append(_fmt(f"{name}_count", k, n))
        return "\n".join(lines) + ("\n" if lines else "")


def render_gauges(gauges: list[tuple[str, dict[str, str], float]]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for name, labels, value in gauges:
        if name not in seen:
            lines.append(f"# TYPE {name} gauge")
            seen.add(name)
        lines.append(_fmt(name, _labels(labels), value))
    return "\n".join(lines) + ("\n" if lines else "")


metrics = Registry()
