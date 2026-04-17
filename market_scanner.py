import requests
import logging
from config import GAMMA_API, MIN_SPREAD_PCT

logger = logging.getLogger(__name__)

def get_top_markets(limit=10):
    """Escanea Polymarket y devuelve top mercados por oportunidad."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active": True,
                "closed": False,
                "limit": 100,
                "order": "volume24hr",
                "ascending": False
            },
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
            volume = float(m.get("volume24hr", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            tokens = m.get("tokens", [])

            if len(tokens) < 2 or volume < 10000 or liquidity < 1000:
                continue

            yes_price = float(tokens[0].get("price", 0) or 0)
            no_price = float(tokens[1].get("price", 0) or 0)

            if yes_price <= 0 or no_price <= 0:
                continue

            spread = abs(1.0 - yes_price - no_price)
            if spread < MIN_SPREAD_PCT:
                continue

            # Score compuesto
            score = (spread * 0.5) + (min(volume / 100000, 1.0) * 0.3) + (min(liquidity / 50000, 1.0) * 0.2)

            opportunities.append({
                "id": m.get("id"),
                "slug": m.get("slug"),
                "title": m.get("question", "")[:80],
                "yes_price": yes_price,
                "no_price": no_price,
                "spread": spread,
                "volume_24h": volume,
                "liquidity": liquidity,
                "score": score,
                "condition_id": m.get("conditionId"),
                "tokens": tokens
            })

        except Exception as e:
            logger.debug(f"Error procesando mercado: {e}")
            continue

    # Top por score
    opportunities.sort(key=lambda x: x["score"], reverse=True)
    top = opportunities[:limit]

    logger.info(f"Escaneados {len(markets)} mercados — {len(opportunities)} oportunidades — top {len(top)}")
    return top
