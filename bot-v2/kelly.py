"""kelly.py - Sizing dinamico con Kelly fraccional."""

import logging
import statistics
from collections import defaultdict, deque

from config import (
    KELLY_FRACTION, KELLY_MIN_VARIANCE, KELLY_VARIANCE_WINDOW,
    MAX_POSITION_PCT, CAPITAL_USDC,
)

logger = logging.getLogger(__name__)


class KellySizer:
    def __init__(self):
        self._history = defaultdict(lambda: deque(maxlen=KELLY_VARIANCE_WINDOW))

    def record_tick(self, market_id, mid):
        try:
            self._history[market_id].append(float(mid))
        except (TypeError, ValueError):
            return

    def _variance(self, market_id):
        series = self._history.get(market_id)
        if not series or len(series) < 5:
            return KELLY_MIN_VARIANCE * 10
        try:
            return max(statistics.pvariance(series), KELLY_MIN_VARIANCE)
        except statistics.StatisticsError:
            return KELLY_MIN_VARIANCE * 10

    def compute_size(self, *, market_id, edge, capital_available, price=None):
        if edge <= 0 or capital_available <= 0:
            return 0.0
        variance = self._variance(market_id)
        raw = (edge / variance) * capital_available * KELLY_FRACTION
        hard_cap = CAPITAL_USDC * MAX_POSITION_PCT
        size = max(0.0, min(raw, hard_cap, capital_available))
        logger.info(
            "Kelly[%s]: edge=%.4f var=%.6f cap=%.2f -> raw=%.2f final=%.2f",
            market_id, edge, variance, capital_available, raw, size,
        )
        return size
