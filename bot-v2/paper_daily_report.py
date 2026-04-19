"""paper_daily_report.py - Reporte diario del modo paper trading.

Se invoca desde main.py una vez por loop. Detecta rollover de dia UTC
y persiste un resumen (win rate, PnL, drawdown) como LogEvent en Base44.
Tambien corta el experimento tras PAPER_DURATION_DAYS.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from config import (
    BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL,
    PAPER_DURATION_DAYS,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _b44_log(payload: dict) -> None:
    if not BASE44_API_KEY:
        return
    url = "%s/api/apps/%s/entities/LogEvent" % (BASE44_BASE_URL, BASE44_APP_ID)
    try:
        requests.post(url, json=payload,
                      headers={"api_key": BASE44_API_KEY,
                               "Content-Type": "application/json"},
                      timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("paper daily log fallo: %s", exc)


class PaperDailyReporter:
    def __init__(self, broker):
        self.broker = broker
        self.started_at = time.time()
        self.current_day = _utc_day(self.started_at)

    def should_stop(self) -> bool:
        elapsed_days = (time.time() - self.started_at) / 86400.0
        return elapsed_days >= float(PAPER_DURATION_DAYS)

    def tick(self) -> None:
        """Llamar 1 vez por ciclo del main loop."""
        today = _utc_day(time.time())
        if today == self.current_day:
            return
        # rollover: cerramos dia anterior, emitimos reporte, reseteamos contadores
        snap = self.broker.snapshot()
        msg = (
            "[PAPER] Daily report %s: pnl=%+.2f equity=%.2f "
            "wins=%d losses=%d win_rate=%.1f%% dd=%.2f"
            % (self.current_day, snap["pnl_today"], snap["equity"],
               snap["wins_today"], snap["losses_today"],
               snap["win_rate_pct"], snap["max_drawdown_usdc"])
        )
        logger.info(msg)
        _b44_log({
            "level": "info",
            "message": msg,
            "module": "paper_daily_report",
            "data": {
                "day": self.current_day,
                "pnl_today": snap["pnl_today"],
                "equity": snap["equity"],
                "wins_today": snap["wins_today"],
                "losses_today": snap["losses_today"],
                "win_rate_pct": snap["win_rate_pct"],
                "max_drawdown_usdc": snap["max_drawdown_usdc"],
                "pnl_total": snap["pnl_total"],
                "open_positions": snap["open_positions"],
            },
        })
        # reset diario (drawdown se mantiene porque es peak-to-trough global)
        self.broker.pnl_today = 0.0
        self.broker.wins_today = 0
        self.broker.losses_today = 0
        self.current_day = today
