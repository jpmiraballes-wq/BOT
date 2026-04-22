"""
market_scanner.py - Escaner de mercados en Polymarket usando Gamma API.

ENDURECIDO_V3 (2026-04-22): agregamos BLACKLIST de mercados direccionales
donde market_maker pierde sistematicamente (WTI, Monero, Anthropic, etc).
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

import requests

from config import GAMMA_API_URL, MIN_SPREAD_PCT

logger = logging.getLogger(__name__)

MIN_VOLUME_USDC = 250_000.0
MIN_LIQUIDITY_USDC = 25_000.0
MAX_DAYS_TO_RESOLUTION = 90
TOP_N = 8
REQUEST_TIMEOUT = 15

# BLACKLIST_V3: mercados direccionales donde MM pierde siempre.
# Perdidas confirmadas: WTI Oil (-$3.26), Monero (-$1.78), Anthropic (-$0.80),
# Bennett (-$1.67). Estos no tienen mean reversion, se comen el SL.
BLACKLIST_PATTERNS = [
    r"\\bhit \\\\\\\$[\\d,]+",
    r"\\breach \\\\\\\$[\\d,]+",
    r"\\bhit \\(low\\) \\\\\\\$",
    r"\\bhit \\(high\\) \\\\\\\$",
    r"\\bwti\\b",
    r"\\bcrude oil\\b",
    r"\\bbrent\\b",
    r"\\bmonero\\b.*\\\\\\\$",
    r"\\blitecoin\\b.*\\\\\\\$",
    r"\\bdogecoin\\b.*\\\\\\\$[\\d]+",
    r"in 202[7-9]",
    r"by 202[7-9]",
]
BLACKLIST_REGEX = re.compile("|".join(BLACKLIST_PATTERNS), re.IGNORECASE)


def _is_blacklisted(question):
    if not question:
        return False
    return bool(BLACKLIST_REGEX.search(question))


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_active_markets(limit=200):
    url = f"{GAMMA_API_URL}/markets"
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": limit, "order": "volume", "ascending": "false",
    }
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        logger.warning("Respuesta inesperada de Gamma API: %s", type(data))
        return []
    except requests.RequestException as exc:
        logger.error("Error consultando Gamma API: %s", exc)
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
                if isinstance(parsed, list) and len(parsed) >= 1:
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
    spread_pct = spread_abs / mid if mid > 0 else 0.0
    return {
        "bid": best_bid, "ask": best_ask, "mid": mid,
        "spread_abs": spread_abs, "spread_pct": spread_pct,
    }


def _days_to_resolution(market):
    end = market.get("endDate") or market.get("end_date_iso") or market.get("endDateIso")
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (dt - now).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def _passes_filters(market, prices):
    # BLACKLIST primero — mercados direccionales
    question = market.get("question") or market.get("slug") or ""
    if _is_blacklisted(question):
        logger.info("Blacklist: rechazado '%s'", question[:60])
        return False

    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    if volume < MIN_VOLUME_USDC:
        return False
    if liquidity < MIN_LIQUIDITY_USDC:
        return False
    if prices["spread_pct"] < MIN_SPREAD_PCT:
        return False
    if prices["mid"] < 0.10 or prices["mid"] > 0.90:
        return False
    days = _days_to_resolution(market)
    if days is not None:
        if days < 0.25:
            return False
        if days > MAX_DAYS_TO_RESOLUTION:
            return False
    return True


def _score(market, prices):
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    mid_balance = 1.0 - abs(prices["mid"] - 0.5) * 2.0
    spread_component = prices["spread_pct"] * 100.0
    liquidity_component = (liquidity / 1000.0)
    return (
        spread_component * 0.5
        + min(liquidity_component, 100.0) * 0.3
        + mid_balance * 20.0 * 0.2
    ) + (volume / 100_000.0)


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
    logger.info("Gamma API devolvio %d mercados candidatos", len(raw_markets))
    opportunities = []
    blacklisted_count = 0
    for market in raw_markets:
        prices = _extract_prices(market)
        if not prices:
            continue
        question = market.get("question") or market.get("slug") or ""
        if _is_blacklisted(question):
            blacklisted_count += 1
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
    logger.info("Escaner V3 filtro %d opps (blacklisted %d); top %d",
                len(opportunities), blacklisted_count, len(top))
    return top
