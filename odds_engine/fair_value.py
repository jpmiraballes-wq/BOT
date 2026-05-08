from __future__ import annotations

from collections import defaultdict
from statistics import median

from models import OddsOutcome


def aggregate_fair_values(outcomes: list[OddsOutcome]) -> dict[tuple[str, str], float]:
    """Return fair value by (external_event_id, normalized outcome name).

    The Odds API may return many bookmakers. We normalize each bookmaker's h2h
    market first, then aggregate with median for robustness.
    """
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for o in outcomes:
        key = (o.external_event_id, o.outcome_name.strip().lower())
        if 0 < o.implied_probability_normalized < 1:
            buckets[key].append(o.implied_probability_normalized)
    return {k: float(median(v)) for k, v in buckets.items() if v}


def event_fair_value_for_name(event_id: str, outcome_name: str, fair_values: dict[tuple[str, str], float]) -> float | None:
    target = outcome_name.strip().lower()
    direct = fair_values.get((event_id, target))
    if direct is not None:
        return direct
    # Fallback: partial name containment for cases like surnames in Polymarket titles.
    for (eid, name), value in fair_values.items():
        if eid == event_id and (target in name or name in target):
            return value
    return None
