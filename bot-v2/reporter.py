"""reporter.py - Envia heartbeats de SystemState a Base44.

Crea un nuevo registro en cada heartbeat (POST). El dashboard lee el mas
reciente con sort=-created_date, limit=1.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

try:
    from config import BOT_VERSION
except ImportError:
    BOT_VERSION = "v2"

try:
    from config import REPORT_INTERVAL_SECONDS
except ImportError:
    REPORT_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class Reporter:
    def __init__(self):
        self._last_report_ts = 0.0
        self._endpoint = "%s/api/apps/%s/entities/SystemState" % (
            BASE44_BASE_URL, BASE44_APP_ID,
        )
        self._headers = {
            "api_key": BASE44_API_KEY or "",
            "Content-Type": "application/json",
        }

    def _due(self):
        return (time.time() - self._last_report_ts) >= REPORT_INTERVAL_SECONDS

    def report(self, snapshot, force=False):
        if not BASE44_API_KEY:
            return
        if not force and not self._due():
            return

        payload = {
            "mode": snapshot.get("mode", "running"),
            "capital_total": float(snapshot.get("capital_total") or 0.0),
            "capital_deployed": float(snapshot.get("capital_deployed") or 0.0),
            "daily_pnl": float(snapshot.get("daily_pnl") or 0.0),
            "total_pnl": float(snapshot.get("total_pnl") or 0.0),
            "drawdown_pct": float(snapshot.get("drawdown_pct") or 0.0),
            "win_rate": float(snapshot.get("win_rate") or 0.0),
            "open_positions": int(snapshot.get("open_positions") or 0),
            "total_trades": int(snapshot.get("total_trades") or 0),
            "uptime_hours": float(snapshot.get("uptime_hours") or 0.0),
            "last_heartbeat": _now_iso(),
            "heartbeat_at": _now_iso(),
            "bot_version": BOT_VERSION,
            "notes": str(snapshot.get("notes") or ""),
        }

        try:
            resp = requests.post(self._endpoint, json=payload,
                                 headers=self._headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("Error reportando a Base44: %d %s",
                             resp.status_code, resp.text[:200])
                return
            self._last_report_ts = time.time()
            logger.info("Heartbeat OK - capital: %s", payload["capital_total"])
        except requests.RequestException as exc:
            logger.error("Error reportando a Base44: %s", exc)

    def send_minimal_heartbeat(self, mode="running", notes=""):
        """Heartbeat de fallback: no depende de rm/om ni del CLOB.

        Uso: cuando build_snapshot() falla (ej: CLOB caido), llamar a esto
        para que el dashboard siga viendo al bot como vivo.
        """
        if not BASE44_API_KEY:
            return
        payload = {
            "mode": mode,
            "capital_total": 0.0,
            "capital_deployed": 0.0,
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "drawdown_pct": 0.0,
            "win_rate": 0.0,
            "open_positions": 0,
            "total_trades": 0,
            "last_heartbeat": _now_iso(),
            "heartbeat_at": _now_iso(),
            "bot_version": BOT_VERSION,
            "notes": "minimal_hb:" + str(notes)[:100],
        }
        try:
            resp = requests.post(self._endpoint, json=payload,
                                 headers=self._headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("minimal_hb HTTP %d: %s",
                             resp.status_code, resp.text[:200])
                return
            self._last_report_ts = time.time()
            logger.warning("Heartbeat MINIMO enviado (build_snapshot fallo)")
        except requests.RequestException as exc:
            logger.error("minimal_hb request fallo: %s", exc)
