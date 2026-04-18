"""logical_arb.py - Arbitraje logico entre mercados hermanos."""

import logging
from collections import defaultdict

import requests

from config import (
    GAMMA_API_URL, LOGICAL_ARB_OVER_THRESHOLD, LOGICAL_ARB_UNDER_THRESHOLD,
)
from base44_client import create_record

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15
MIN_GROUP_SIZE = 2
MAX_GROUP_SIZE = 20


def _safe_float(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _extract_yes_price(market):
    bid = _safe_float(market.get("bestBid"))
    ask = _safe_float(market.get("bestAsk"))
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, str):
        try:
            import json as _json
            parsed = _json.loads(outcome_prices)
            if isinstance(parsed, list) and parsed:
                p = _safe_float(parsed[0])
                if 0 < p < 1:
                    return p
        except (ValueError, TypeError):
            return None
    return None


def _group_key(market):
    parent = (market.get("parent_market") or market.get("parentMarket")
              or market.get("eventId") or market.get("event_id"))
    if parent:
        return "parent:" + str(parent)
    category = market.get("category")
    event_title = market.get("eventTitle") or market.get("event_title")
    if category and event_title:
        return "evt:" + str(category) + ":" + str(event_title)
    return None


def _fetch_active_markets(limit=500):
    url = GAMMA_API_URL + "/markets"
    params = {"active": "true", "closed": "false", "archived": "false",
              "limit": limit, "order": "volume", "ascending": "false"}
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []
    except requests.RequestException as exc:
        logger.error("Gamma API error (logical_arb): %s", exc)
        return []


def _persist_opportunity(*, group_key, total_yes, direction, markets):
    if direction == "sell":
        profit_per_100 = (total_yes - 1.0) * 100.0 / total_yes
    else:
        profit_per_100 = (1.0 - total_yes) * 100.0 / total_yes
    spread_pct = abs(total_yes - 1.0)
    titles = [m.get("question") or m.get("slug") or "" for m in markets]
    market_title = " | ".join(t[:40] for t in titles[:3])
    if len(titles) > 3:
        market_title += " (+%d)" % (len(titles) - 3)
    create_record("Opportunity", {
        "market_title": market_title,
        "spread_pct": round(spread_pct, 4),
        "profit_per_100": round(profit_per_100, 3),
        "arb_type": "logical_arb_" + direction,
        "status": "detected",
    })
    logger.info(
        "Arbitraje logico [%s]: sum(YES)=%.4f dir=%s +%.2f/100 USDC",
        group_key, total_yes, direction, profit_per_100,
    )


def scan_logical_arb():
    markets = _fetch_active_markets()
    groups = defaultdict(list)
    for m in markets:
        key = _group_key(m)
        if not key:
            continue
        yes = _extract_yes_price(m)
        if yes is None:
            continue
        m["_yes"] = yes
        groups[key].append(m)

    opportunities = []
    for key, members in groups.items():
        if not (MIN_GROUP_SIZE <= len(members) <= MAX_GROUP_SIZE):
            continue
        total_yes = sum(m["_yes"] for m in members)
        direction = None
        if total_yes > LOGICAL_ARB_OVER_THRESHOLD:
            direction = "sell"
        elif total_yes < LOGICAL_ARB_UNDER_THRESHOLD:
            direction = "buy"
        if not direction:
            continue
        _persist_opportunity(group_key=key, total_yes=total_yes,
                             direction=direction, markets=members)
        opportunities.append({
            "group_key": key, "direction": direction,
            "total_yes": total_yes, "members": members,
        })

    if opportunities:
        logger.info("Arbitraje logico: %d oportunidades", len(opportunities))
    return opportunities
