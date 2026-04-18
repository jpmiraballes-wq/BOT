import requests
import logging

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
MIN_SPREAD = 0.001
MIN_VOLUME = 500
MIN_LIQUIDITY = 50


def scan_markets(limit=10):
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"active": True, "closed": False, "limit": 100,
                    "order": "volume24hr", "ascending": False, "enableOrderBook": True},
            timeout=10
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        logger.error(f"Error escaneando mercados: {e}")
        return []

    opportunities = []
    for m in markets:
        try:
            vol = float(m.get("volume24hr") or 0)
            liq = float(m.get("liquidityClob") or m.get("liquidity") or 0)
            bid = float(m.get("bestBid") or 0)
            ask = float(m.get("bestAsk") or 0)
            accepting = m.get("acceptingOrders", False)
            if not accepting:
                continue
            if vol < MIN_VOLUME:
                continue
            if liq < MIN_LIQUIDITY:
                continue
            if bid <= 0 or ask <= 0:
                continue
            if ask <= bid:
                continue
            spread = ask - bid
            if spread < MIN_SPREAD:
                continue
            mid = (bid + ask) / 2.0
            if mid < 0.005 or mid > 0.995:
                continue
            clob_ids = m.get("clobTokenIds", [])
            outcome_prices = m.get("outcomePrices", [])
            yes_price = float(outcome_prices[0]) if outcome_prices else mid
            no_price = float(outcome_prices[1]) if len(outcome_prices) > 1 else (1.0 - mid)
            score = (spread * 0.4) + (min(vol / 100000, 1.0) * 0.35) + (min(liq / 50000, 1.0) * 0.25)
            opportunities.append({
                "market_id": m.get("conditionId") or m.get("id"),
                "id": m.get("id"),
                "question": m.get("question", "")[:80],
                "title": m.get("question", "")[:80],
                "yes_price": yes_price,
                "no_price": no_price,
                "mid": mid,
                "best_bid": bid,
                "best_ask": ask,
                "spread": spread,
                "spread_pct": spread,
                "volume_24h": vol,
                "liquidity": liq,
                "score": score,
                "condition_id": m.get("conditionId"),
                "end_date_iso": m.get("endDateIso") or m.get("endDate"),
                "token_id_yes": clob_ids[0] if clob_ids else "",
                "token_id_no": clob_ids[1] if len(clob_ids) > 1 else "",
                "tokens": [
                    {"token_id": clob_ids[0] if clob_ids else "", "outcome": "Yes", "price": yes_price},
                    {"token_id": clob_ids[1] if len(clob_ids) > 1 else "", "outcome": "No", "price": no_price},
                ]
            })
        except Exception as e:
            logger.debug(f"Error procesando mercado: {e}")
            continue

    opportunities.sort(key=lambda x: x["score"], reverse=True)
    top = opportunities[:limit]
    logger.info(f"Escaneados {len(markets)} mercados — {len(opportunities)} oportunidades — top {len(top)}")
    return top


get_top_markets = scan_markets
