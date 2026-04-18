"""
market_scanner.py - Escaner de mercados Polymarket via Gamma API (v2).

Filtra mercados por volumen, liquidez y spread minimos, y puntua las
oportunidades mas prometedoras para market making. Devuelve una lista
ordenada con toda la informacion necesaria para el OrderManager.

Thresholds pensados para cartera pequena (30 USDC):
  MIN_SPREAD     = 0.001  (0.1%)
  MIN_VOLUME     = 500    USDC
  MIN_LIQUIDITY  = 50     USDC

Cada entrada devuelta contiene:
  - market_id, question, category
  - bid, ask, mid, spread_pct
  - volume, liquidity
  - token_ids  (clobTokenIds)
  - score      (heuristica de ranking)
  - raw        (payload completo para filtros posteriores, p.ej. circuit breakers)
"""

import json
import logging
from typing import Any, Dict, List, Optional

import requests

from config import GAMMA_API_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Umbrales
# ---------------------------------------------------------------------------
MIN_SPREAD = 0.001      # 0.1% spread minimo
MIN_VOLUME = 500.0      # USDC
MIN_LIQUIDITY = 50.0    # USDC
EXTREME_LOW = 0.05      # descartar mercados con mid fuera de [0.05, 0.95]
EXTREME_HIGH = 0.95
DEFAULT_LIMIT = 500
TOP_N = 20
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _parse_token_ids(market):
    """Extrae clobTokenIds como lista de strings."""
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except (ValueError, TypeError):
            return []
    return []


def _extract_prices(market):
    """Devuelve (bid, ask, mid) usando bestBid/bestAsk de la Gamma API."""
    bid = _safe_float(market.get("bestBid"))
    ask = _safe_float(market.get("bestAsk"))

    # Fallback a outcomePrices si no hay best bid/ask.
    if (bid <= 0 or ask <= 0 or ask < bid):
        op = market.get("outcomePrices")
        if isinstance(op, str):
            try:
                parsed = json.loads(op)
                if isinstance(parsed, list) and parsed:
                    yes = _safe_float(parsed[0])
                    if 0 < yes < 1:
                        spread = 0.01
                        bid = max(0.01, yes - spread / 2)
                        ask = min(0.99, yes + spread / 2)
            except (ValueError, TypeError):
                pass

    if bid <= 0 or ask <= 0 or ask < bid:
        return 0.0, 0.0, 0.0
    mid = (bid + ask) / 2.0
    return bid, ask, mid


def _passes_filters(bid, ask, mid, volume, liquidity):
    if bid <= 0 or ask <= 0 or mid <= 0:
        return False
    if mid < EXTREME_LOW or mid > EXTREME_HIGH:
        return False
    if volume < MIN_VOLUME:
        return False
    if liquidity < MIN_LIQUIDITY:
        return False
    spread_pct = (ask - bid) / mid
    if spread_pct < MIN_SPREAD:
        return False
    return True


def _score(spread_pct, liquidity, mid):
    """Heuristica simple: premia spread amplio, liquidez y mercados balanceados."""
    balance_bonus = 1.0 - abs(mid - 0.5) * 2.0  # 1.0 en 0.5, 0.0 en 0 o 1
    return (spread_pct * 100.0) * (liquidity / 100.0) * (0.5 + balance_bonus / 2.0)


# ---------------------------------------------------------------------------
# Gamma API
# ---------------------------------------------------------------------------
def _fetch_markets(limit=DEFAULT_LIMIT):
    url = GAMMA_API_URL.rstrip("/") + "/markets"
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "order": "volume",
        "ascending": "false",
    }
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []
    except requests.RequestException as exc:
        logger.error("Gamma API error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def scan_markets(limit=DEFAULT_LIMIT, top_n=TOP_N):
    """
    Escanea la Gamma API y devuelve hasta `top_n` oportunidades validas.
    """
    markets = _fetch_markets(limit=limit)
    logger.info("Gamma API: %d mercados descargados", len(markets))

    opportunities = []
    for m in markets:
        try:
            bid, ask, mid = _extract_prices(m)
            volume = _safe_float(m.get("volume") or m.get("volumeNum"))
            liquidity = _safe_float(m.get("liquidity") or m.get("liquidityNum"))

            if not _passes_filters(bid, ask, mid, volume, liquidity):
                continue

            token_ids = _parse_token_ids(m)
            if not token_ids:
                continue

            spread_pct = (ask - bid) / mid
            opportunities.append({
                "market_id": str(m.get("id") or m.get("conditionId") or m.get("slug")),
                "question": m.get("question") or m.get("slug") or "",
                "category": m.get("category") or "",
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_pct": spread_pct,
                "volume": volume,
                "liquidity": liquidity,
                "token_ids": token_ids,
                "score": _score(spread_pct, liquidity, mid),
                "raw": m,
            })
        except Exception as exc:
            logger.debug("Mercado descartado por error: %s", exc)
            continue

    opportunities.sort(key=lambda o: o["score"], reverse=True)
    top = opportunities[:top_n]
    logger.info(
        "Oportunidades validas: %d (top %d devueltas)",
        len(opportunities), len(top),
    )
    return top


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for opp in scan_markets():
        print("%.4f spread | vol=%.0f liq=%.0f | %s" % (
            opp["spread_pct"], opp["volume"], opp["liquidity"], opp["question"][:60],
        ))
