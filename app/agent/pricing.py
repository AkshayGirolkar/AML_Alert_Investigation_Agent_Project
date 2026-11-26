"""Model price table (USD per 1M tokens). Cost is computed here, never by the LLM.

Prices are a snapshot: check them against the provider's pricing page before relying on
the numbers for budgeting. An unknown model costs 0 and is logged, so a new model name
never breaks an investigation (it just goes untracked until added here).
"""

import logging

log = logging.getLogger(__name__)

# model -> (input, output) USD per 1M tokens
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "synthetic-llm": (0.15, 0.60),  # F16 fake provider: billed like gpt-4o-mini
}

_warned: set[str] = set()


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = PRICES.get(model)
    if prices is None:
        # dated snapshots such as gpt-4o-2024-08-06 use the base model's price
        prices = next(
            (
                p
                for name, p in sorted(PRICES.items(), key=lambda kv: -len(kv[0]))
                if model.startswith(name)
            ),
            None,
        )
    if prices is None:
        if model not in _warned:
            _warned.add(model)
            log.warning("no price for model %r; its cost is recorded as 0", model)
        return 0.0
    return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000
