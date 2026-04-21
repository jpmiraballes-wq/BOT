"""market_scanner.py - Escaner de mercados + arbitraje logico intra-mercado.

Funciones principales:
  - scan_markets(): oportunidades de market-making (volumen >= 50k).
  - scan_logical_arb(): detecta mercados donde YES + NO < 0.97, que permite
    comprar ambos lados y cobrar la diferencia cuando converjan a 1.0.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import requests

from config import GAMMA_API_URL, MIN_SPREAD_PCT

logger = logging.getLogger(__name__)

MIN_VOLUME_USDC = 20_000.0
MIN_LIQUIDITY_USDC = 2_000.0
TOP_N = 40
REQUEST_TIMEOUT = 15

# Minimo de dias hasta la resolucion del mercado. Si falta menos que esto,
# evitamos entrar (no hay tiempo para que el trade se mueva a favor y el
# riesgo de ir a cero por resolucion es alto). Ref: Sidemen Charity Match 2026.
MIN_DAYS_TO_RESOLUTION = 7

# Umbral de arbitraje logico: sum(YES+NO) debe ser menor a este valor.
LOGICAL_ARB_THRESHOLD = 0.97
LOGICAL_ARB_MIN_VOLUME = 20_000.0
LOGICAL_ARB_TOP_N = 10


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _hours_to_resolution(market):
    """Devuelve horas hasta endDate del mercado. None si no se puede parsear.

    Gamma API suele devolver endDate como ISO 8601 (ej: "2026-06-15T00:00:00Z").
    """
    from datetime import datetime, timezone
    raw = market.get("endDate") or market.get("endDateIso")
    if not raw:
        return None
    try:
        # Soporta sufijo Z y offsets.
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _fetch_active_markets(limit=500):
    url = "%s/markets" % GAMMA_API_URL
    params = {"active": "true", "closed": "false", "archived": "false",
              "limit": limit, "order": "volume", "ascending": "false"}
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
                parsed = json.loads(outcome_prices)
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


def _extract_yes_no_prices(market):
    """Devuelve (yes_ask, no_ask) desde outcomePrices si es binario."""
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not (isinstance(raw, list) and len(raw) == 2):
        return None
    yes = _safe_float(raw[0])
    no = _safe_float(raw[1])
    if yes <= 0 or no <= 0:
        return None
    return yes, no


def _get_dynamic_thresholds():
    """LIQUIDITY_FILTER_V1: lee min_volume_usdc y min_liquidity_usdc de BotConfig.

    Fallback a las constantes hardcoded si BotConfig no esta disponible o
    no tiene los campos. Se cachea por la duracion de fetch_bot_config().
    """
    try:
        from bot_config_reader import fetch_bot_config
        cfg = fetch_bot_config() or {}
        min_vol = cfg.get("min_volume_usdc")
        min_liq = cfg.get("min_liquidity_usdc")
        return (
            float(min_vol) if min_vol is not None else MIN_VOLUME_USDC,
            float(min_liq) if min_liq is not None else MIN_LIQUIDITY_USDC,
        )
    except Exception:
        return MIN_VOLUME_USDC, MIN_LIQUIDITY_USDC


def _passes_filters(market, prices):
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    min_vol, min_liq = _get_dynamic_thresholds()
    if volume < min_vol:
        return False
    if liquidity < min_liq:
        return False
    if prices["spread_pct"] < MIN_SPREAD_PCT:
        return False
    # PRICE_RANGE_V2: subimos a 0.20-0.80. En <0.20 o >0.80 el tick de 0.01
    # es >5% del precio y cualquier SL -7% se gatilla con 1 solo tick.
    if prices["mid"] < 0.20 or prices["mid"] > 0.80:
        return False
    hours_left = _hours_to_resolution(market)
    if hours_left is not None and hours_left < MIN_DAYS_TO_RESOLUTION * 24:
        logger.info("skip resolution<%dd: %s resuelve en %.1fh",
                    MIN_DAYS_TO_RESOLUTION,
                    (market.get("question") or "")[:60],
                    hours_left)
        return False
    return True


def _score(market, prices):
    volume = _safe_float(market.get("volume") or market.get("volumeNum"))
    liquidity = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    mid_balance = 1.0 - abs(prices["mid"] - 0.5) * 2.0
    return (prices["spread_pct"] * 100.0 * 0.5
            + min(liquidity / 1000.0, 100.0) * 0.3
            + mid_balance * 20.0 * 0.2
            + volume / 100_000.0)


def _extract_token_ids(market):
    raw = market.get("clobTokenIds")
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


# ---------------------------------------------------------------------------
# Arbitraje logico intra-mercado (YES + NO < 0.97)
# ---------------------------------------------------------------------------
def scan_logical_arb():
    """Detecta mercados binarios donde YES + NO < LOGICAL_ARB_THRESHOLD.

    Estrategia: comprar ambos lados (YES y NO) y esperar que sumen 1.0 al
    resolver. Profit por 100 USDC desplegados = (1 - total) / total * 100.
    """
    raw_markets = _fetch_active_markets()
    opportunities = []

    for market in raw_markets:
        volume = _safe_float(market.get("volume") or market.get("volumeNum"))
        if volume < LOGICAL_ARB_MIN_VOLUME:
            continue
        hours_left = _hours_to_resolution(market)
        if hours_left is not None and hours_left < MIN_DAYS_TO_RESOLUTION * 24:
            continue
        yn = _extract_yes_no_prices(market)
        if not yn:
            continue
        yes_price, no_price = yn
        total = yes_price + no_price
        if total >= LOGICAL_ARB_THRESHOLD or total <= 0:
            continue
        token_ids = _extract_token_ids(market)
        if len(token_ids) < 2:
            continue

        profit_per_100 = (1.0 - total) / total * 100.0
        opportunities.append({
            "market_id": market.get("id") or market.get("conditionId"),
            "question": market.get("question") or market.get("slug"),
            "arb_type": "logical_arb",
            "action": "buy_both_sides",
            "yes_price": yes_price,
            "no_price": no_price,
            "total": total,
            "spread_pct": round(1.0 - total, 4),
            "profit_per_100": round(profit_per_100, 3),
            "volume": volume,
            "token_ids": token_ids,  # [yes_token_id, no_token_id]
            "raw": market,
        })

    opportunities.sort(key=lambda x: x["profit_per_100"], reverse=True)
    top = opportunities[:LOGICAL_ARB_TOP_N]
    logger.info("Arbitraje logico: %d mercados con YES+NO < %.2f (top %d)",
                len(opportunities), LOGICAL_ARB_THRESHOLD, len(top))
    return top
