"""bot_config_reader.py - Lee BotConfig desde Base44 con cache corto.

Expone:
  fetch_bot_config(force=False) -> dict con campos normalizados.

Campos que devuelve (si estan en el record):
  paused (bool), emergency_stop (bool), emergency_stop_at (str),
  capital_usdc (float), max_position_pct (float), min_spread_pct (float),
  stop_loss (float), take_profit (float), mode (str),
  strategy_market_maker (bool), strategy_logical_arb (bool),
  strategy_prob_arbitrage (bool), strategy_momentum (bool),
  id (str), updated_date (str).

Cache: CACHE_TTL_SECONDS (30s). force=True lo invalida.
"""

import logging
import time
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 10
CACHE_TTL_SECONDS = 30

_CACHE: Dict[str, Any] = {}
_CACHE_TS: float = 0.0


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "t")
    return default


def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize(rec: Dict[str, Any]) -> Dict[str, Any]:
    if not rec:
        return {}
    out: Dict[str, Any] = {
        "id": rec.get("id"),
        "updated_date": rec.get("updated_date"),
        "paused": _as_bool(rec.get("paused"), False),
        "emergency_stop": _as_bool(rec.get("emergency_stop"), False),
        "emergency_stop_at": rec.get("emergency_stop_at"),
        "mode": rec.get("mode"),
        "strategy_market_maker": _as_bool(rec.get("strategy_market_maker"), True),
        "strategy_logical_arb": _as_bool(rec.get("strategy_logical_arb"), False),
        "strategy_prob_arbitrage": _as_bool(rec.get("strategy_prob_arbitrage"), False),
        "strategy_momentum": _as_bool(rec.get("strategy_momentum"), False),
    }
    for k in ("capital_usdc", "max_position_pct", "min_spread_pct",
              "stop_loss", "take_profit", "max_open_orders",
              "rebalance_interval_sec"):
        v = _as_float(rec.get(k))
        if v is not None:
            out[k] = v
    return out


def fetch_bot_config(force: bool = False) -> Dict[str, Any]:
    """Devuelve el BotConfig mas reciente, con cache de 30s."""
    global _CACHE, _CACHE_TS
    if not BASE44_API_KEY:
        return {}
    now = time.time()
    if not force and _CACHE and (now - _CACHE_TS) < CACHE_TTL_SECONDS:
        return _CACHE

    url = "%s/api/apps/%s/entities/BotConfig" % (BASE44_BASE_URL, BASE44_APP_ID)
    params = {"sort": "-created_date", "limit": 1}
    headers = {"api_key": BASE44_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("BotConfig fetch %d: %s", resp.status_code, resp.text[:200])
            return _CACHE  # mantiene cache previo
        data = resp.json()
        records = data if isinstance(data, list) else (
            data.get("data") or data.get("records") or []
        )
        rec = records[0] if records else {}
        normalized = _normalize(rec)
        if normalized:
            _CACHE = normalized
            _CACHE_TS = now
        return normalized or _CACHE
    except requests.RequestException as exc:
        logger.warning("BotConfig fetch fallo: %s", exc)
        return _CACHE
