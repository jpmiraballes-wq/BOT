"""market_scanner.py - Escaner de mercados en Polymarket via Gamma API.

Filtra mercados activos por volumen (>=50k), liquidez y spread, y devuelve
las mejores oportunidades ordenadas por un score compuesto.
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from config import GAMMA_API_URL, MIN_SPREAD_PCT

logger = logging.getLogger(__name__)

# Filtros minimos para considerar un mercado.
MIN_VOLUME_USDC = 50_000.0   # evita mercados ilquidos / de baja actividad
MIN_LIQUIDITY_USDC = 5_000.0
TOP_N = 20
REQUEST_TIMEOUT = 15


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_active_markets(limit=500):
    url = "%s/markets" % GAMMA_API_URL
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": limit, "order": "volume", "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        return []
    except requests.RequestException as exc:
        logger.error("Gamma API error: %s", exc)
        return []


def _extract_prices(market):
    best_bid = _safe_float(market.get("bestBid"))
    best_ask = _safe_float(market.get("bestAsk"))
    if best_bid <= 0 or best_ask <= 0:
        outcome_prices = market.get("outcomePrices")
        if isinstance(outcome_prices, str):
            try:
                import json as _json
                parsed = _json.loads(outcome_prices)
                if isinstance(parsed, list) and parsed:
                    price = _safe_float(parsed[0])
                    if 0 < price < 1:
                        best_bid = max(0.01, price - 0.01)
                        best_ask = min(0.99, price + 0.01)
            except (ValueError, TypeError):
                pass
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    spread_abs = best_ask - best_bid
    return {"bid": best_bid, "ask": best_ask, "mid": mid,
            "spread_abs": spread_abs,
            "spread_pct": spread_abs / mid if mid > 0 else 0.0}


def _passes_filters(market, prices):
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    if volume < MIN_VOLUME_USDC:
        return False
    if liquidity < MIN_LIQUIDITY_USDC:
        return False
    if prices["spread_pct"] < MIN_SPREAD_PCT:
        return False
    if prices["mid"] < 0.05 or prices["mid"] > 0.95:
        return False
    return True


def _score(market, prices):
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    mid_balance = 1.0 - abs(prices["mid"] - 0.5) * 2.0
    spread_component = prices["spread_pct"] * 100.0
    liquidity_component = liquidity / 1000.0
    return (spread_component * 0.5
            + min(liquidity_component, 100.0) * 0.3
            + mid_balance * 20.0 * 0.2
            + volume / 100_000.0)


def _extract_token_ids(market):
    token_ids_raw = market.get("clobTokenIds")
    if isinstance(token_ids_raw, list):
        return [str(t) for t in token_ids_raw if t]
    if isinstance(token_ids_raw, str):
        try:
            import json as _json
            parsed = _json.loads(token_ids_raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except (ValueError, TypeError):
            return []
    return []


def scan_markets():
    raw_markets = _fetch_active_markets()
    logger.info("Gamma API: %d mercados descargados", len(raw_markets))

    opportunities = []
    for market in raw_markets:
        prices = _extract_prices(market)
        if not prices:
            continue
        if not _passes_filters(market, prices):
            continue
        token_ids = _extract_token_ids(market)
        if not token_ids:
            continue
        opportunities.append({
            "market_id": market.get("id") or market.get("conditionId"),
            "condition_id": market.get("conditionId"),
            "question": market.get("question") or market.get("slug"),
            "bid": prices["bid"], "ask": prices["ask"], "mid": prices["mid"],
            "spread_pct": prices["spread_pct"],
            "volume": _safe_float(market.get("volume") or market.get("volumeNum")),
            "liquidity": _safe_float(market.get("liquidity") or market.get("liquidityNum")),
            "token_ids": token_ids,
            "score": _score(market, prices),
            "raw": market,
        })

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    top = opportunities[:TOP_N]
    logger.info("Oportunidades validas: %d (top %d devueltas)",
                len(opportunities), len(top))
    return top
