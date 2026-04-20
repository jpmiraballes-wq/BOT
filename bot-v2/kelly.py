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
        # Caps dinamicos por estrategia (cargados desde Base44 StrategySizing).
        # Si una estrategia no esta aqui, se usa MAX_POSITION_PCT de config.
        self.strategy_caps = {}

    def set_strategy_caps(self, caps):
        """Actualiza los caps. caps = {'market_maker': 0.04, ...}"""
        if isinstance(caps, dict):
            self.strategy_caps = {k: float(v) for k, v in caps.items() if v}
            logger.info("Kelly strategy_caps actualizado: %s", self.strategy_caps)

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

    def compute_size(self, *, market_id, edge, capital_available, price=None, strategy=None):
        if edge <= 0 or capital_available <= 0:
            return 0.0
        variance = self._variance(market_id)
        raw = (edge / variance) * capital_available * KELLY_FRACTION
        # Cap dinamico por estrategia si existe, sino MAX_POSITION_PCT
        cap_pct = self.strategy_caps.get(strategy, MAX_POSITION_PCT) if strategy else MAX_POSITION_PCT
        hard_cap = CAPITAL_USDC * cap_pct
        size = max(0.0, min(raw, hard_cap, capital_available))
        logger.info(
            "Kelly[%s strat=%s cap=%.2f%%]: edge=%.4f var=%.6f avail=%.2f -> raw=%.2f final=%.2f",
            market_id, strategy or "-", cap_pct * 100.0,
            edge, variance, capital_available, raw, size,
        )
        return size
