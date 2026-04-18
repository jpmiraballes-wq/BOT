"""decision_logger.py - Registra decisiones en LogEvent de Base44."""

import logging
from typing import Any, Dict, Optional

from base44_client import create_record, now_iso

logger = logging.getLogger(__name__)


def _emit(level, message, module, data):
    logger.info("[%s] %s | %s", module, message, data)
    create_record("LogEvent", {
        "level": level, "message": message, "module": module, "data": data,
    })


def log_decision(*, reason, market, strategy, edge=None, size=None, extra=None):
    data = {"reason": reason, "market": market, "strategy": strategy,
            "timestamp": now_iso()}
    if edge is not None:
        data["edge"] = float(edge)
    if size is not None:
        data["size"] = float(size)
    if extra:
        data.update(extra)
    _emit("info", "decision:" + reason, module=strategy, data=data)


def log_close(*, market, strategy, pnl, duration_sec=None, reason="close", extra=None):
    data = {"reason": reason, "market": market, "strategy": strategy,
            "pnl": float(pnl), "timestamp": now_iso()}
    if duration_sec is not None:
        data["duration_sec"] = float(duration_sec)
    if extra:
        data.update(extra)
    level = "win" if pnl > 0 else ("error" if pnl < 0 else "info")
    _emit(level, "close:" + reason, module=strategy, data=data)


def log_warning(message, module, extra=None):
    payload = dict(extra or {})
    payload["timestamp"] = now_iso()
    _emit("warn", message, module=module, data=payload)
