"""circuit_breakers.py - Controles reactivos del loop."""

import logging
import time
from datetime import datetime, timezone

from config import (
    EXTREME_HIGH, EXTREME_LOW, EXTREME_SIZE_FACTOR,
    INTRADAY_DD_PAUSE_PCT, INTRADAY_DD_PAUSE_SECONDS,
    MIN_HOURS_TO_RESOLUTION,
)

logger = logging.getLogger(__name__)


class CircuitBreakers:
    def __init__(self):
        self._pause_until_ts = 0.0
        self._day_anchor_equity = None
        self._day_anchor_date = None

    def _today_key(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def update_equity(self, equity):
        today = self._today_key()
        if self._day_anchor_date != today or self._day_anchor_equity is None:
            self._day_anchor_date = today
            self._day_anchor_equity = float(equity)
            return
        anchor = self._day_anchor_equity
        if anchor <= 0:
            return
        dd = (anchor - float(equity)) / anchor
        if dd >= INTRADAY_DD_PAUSE_PCT and not self.is_paused():
            self._pause_until_ts = time.time() + INTRADAY_DD_PAUSE_SECONDS
            logger.warning(
                "Circuit breaker intradia: dd=%.2f%% >= %.2f%%",
                dd * 100, INTRADAY_DD_PAUSE_PCT * 100,
            )

    def is_paused(self):
        return time.time() < self._pause_until_ts

    def seconds_until_resume(self):
        return max(0, int(self._pause_until_ts - time.time()))

    @staticmethod
    def get_size_factor(mid_price):
        if mid_price < EXTREME_LOW or mid_price > EXTREME_HIGH:
            return EXTREME_SIZE_FACTOR
        return 1.0

    @staticmethod
    def resolution_imminent(market):
        raw = (market.get("end_date_iso") or market.get("endDate")
               or market.get("endDateIso") or market.get("end_date"))
        if not raw:
            return False
        try:
            iso = raw.replace("Z", "+00:00") if isinstance(raw, str) else raw
            end = datetime.fromisoformat(iso)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            hours = (end - datetime.now(timezone.utc)).total_seconds() / 3600.0
            return hours < MIN_HOURS_TO_RESOLUTION
        except (ValueError, AttributeError, TypeError):
            return False

    @classmethod
    def filter_opportunity(cls, opp):
        mid = float(opp.get("mid") or 0.0)
        if mid <= 0:
            return False
        if cls.resolution_imminent(opp.get("raw") or opp):
            return False
        return True
