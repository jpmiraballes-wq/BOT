"""logical_arb.py - Deteccion de inconsistencias matematicas en Polymarket.

Tres familias de arbitraje logico:

  1) BINARY_UNDER: mercado binario donde P(YES) + P(NO) < UNDER_THRESHOLD.
     Comprar ambos lados = profit garantizado a la resolucion.

  2) UMBRELLA_OVER_CHILDREN: un mercado "any of X" cotiza mas barato que la
     suma de sus componentes individuales. Ej: "AFC team wins SB" = 24%
     pero suma(equipos AFC) = 28%. Comprar umbrella, vender children.

  3) MONOTONIC_VIOLATION: en grupos con thresholds anidados (BTC >$100k,
     >$120k, >$150k) el precio debe ser monotonicamente decreciente. Si
     >$120k cotiza mas caro que >$100k, arbitraje puro.

El modulo solo DETECTA y reporta a Base44 como Opportunity. La ejecucion
concreta queda para una futura estrategia LogicalArbStrategy que consumira
capital de StrategyCapital['logical_arb']. Mantener detector y ejecutor
separados ayuda a validar con dinero virtual primero.
"""

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL, GAMMA_API_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15

# Umbrales
BINARY_UNDER_THRESHOLD = 0.97         # YES+NO < 0.97 -> arb
UMBRELLA_MIN_DIFF = 0.03              # sum(children) - umbrella >= 3%
MONOTONIC_MIN_DIFF = 0.02             # violacion minima 2% para reportar
MIN_VOLUME_USDC = 10_000.0            # ignora mercados ilquidos
MIN_LIQUIDITY_USDC = 1_000.0
TOP_N = 20

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fetch_markets(limit=500):
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
        return data if isinstance(data, list) else []
    except requests.RequestException as exc:
        logger.error("Gamma API error: %s", exc)
        return []


def _extract_outcome_prices(market) -> Optional[List[float]]:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, list):
        return None
    prices = [_safe_float(p) for p in raw]
    return prices if all(p >= 0 for p in prices) else None


def _extract_token_ids(market) -> List[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    return []


def _volume_ok(market) -> bool:
    vol = _safe_float(market.get("volume") or market.get("volumeNum"))
    liq = _safe_float(market.get("liquidity") or market.get("liquidityNum"))
    return vol >= MIN_VOLUME_USDC and liq >= MIN_LIQUIDITY_USDC


def _is_binary(prices) -> bool:
    return prices is not None and len(prices) == 2 and all(0 < p < 1 for p in prices)


# ---------------------------------------------------------------------------
# 1) BINARY_UNDER: YES + NO < 0.97
# ---------------------------------------------------------------------------
def _scan_binary_under(markets) -> List[Dict[str, Any]]:
    opps = []
    for m in markets:
        if not _volume_ok(m):
            continue
        prices = _extract_outcome_prices(m)
        if not _is_binary(prices):
            continue
        yes, no = prices[0], prices[1]
        total = yes + no
        if total >= BINARY_UNDER_THRESHOLD or total <= 0:
            continue
        token_ids = _extract_token_ids(m)
        if len(token_ids) < 2:
            continue
        edge = 1.0 - total
        opps.append({
            "arb_type": "binary_under",
            "market_id": m.get("id") or m.get("conditionId"),
            "question": m.get("question") or m.get("slug"),
            "yes_price": yes, "no_price": no, "total": round(total, 4),
            "edge_pct": round(edge * 100, 3),
            "profit_per_100": round(edge / total * 100, 3),
            "token_ids": token_ids,
            "volume": _safe_float(m.get("volume") or m.get("volumeNum")),
        })
    return opps


# ---------------------------------------------------------------------------
# 2) UMBRELLA_OVER_CHILDREN
# Agrupa mercados por 'eventSlug' o 'groupSlug'. Si uno se titula "Any X" /
# "Will any..." / tiene 'anywinner' en el slug, lo tomamos como umbrella.
# ---------------------------------------------------------------------------
UMBRELLA_PATTERNS = re.compile(
    r"(anys+w+s+win|wills+any|anys+other|field|others+candidate)",
    re.IGNORECASE,
)


def _group_key(market) -> Optional[str]:
    return (market.get("eventSlug") or market.get("groupSlug")
            or market.get("event_slug") or market.get("group_slug"))


def _yes_price(market) -> Optional[float]:
    prices = _extract_outcome_prices(market)
    if not _is_binary(prices):
        return None
    return prices[0]


def _scan_umbrella(markets) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for m in markets:
        key = _group_key(m)
        if not key:
            continue
        if not _volume_ok(m):
            continue
        if not _is_binary(_extract_outcome_prices(m)):
            continue
        groups[key].append(m)

    opps = []
    for key, members in groups.items():
        if len(members) < 3:
            continue  # umbrella + al menos 2 children

        umbrella = None
        children = []
        for m in members:
            question = (m.get("question") or "").strip()
            if UMBRELLA_PATTERNS.search(question):
                umbrella = m
            else:
                children.append(m)
        if not umbrella or len(children) < 2:
            continue

        u_price = _yes_price(umbrella)
        if u_price is None:
            continue
        child_sum = sum(_yes_price(c) or 0.0 for c in children)
        diff = child_sum - u_price
        if diff < UMBRELLA_MIN_DIFF:
            continue

        opps.append({
            "arb_type": "umbrella_over_children",
            "group_key": key,
            "umbrella_market_id": umbrella.get("id"),
            "umbrella_question": umbrella.get("question"),
            "umbrella_price": round(u_price, 4),
            "children_sum": round(child_sum, 4),
            "children_count": len(children),
            "edge_pct": round(diff * 100, 3),
            "action": "buy_umbrella_sell_children",
        })
    return opps


# ---------------------------------------------------------------------------
# 3) MONOTONIC_VIOLATION
# Detecta grupos tipo "BTC above $X on date Y" con thresholds crecientes.
# ---------------------------------------------------------------------------
THRESHOLD_PATTERN = re.compile(r"$?([d,]+(?:.d+)?)s*(k|m|bn)?", re.IGNORECASE)


def _extract_threshold(question: str) -> Optional[float]:
    if not question:
        return None
    m = THRESHOLD_PATTERN.search(question)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "k":
        value *= 1_000
    elif unit == "m":
        value *= 1_000_000
    elif unit == "bn":
        value *= 1_000_000_000
    return value


def _scan_monotonic(markets) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Tuple[float, Dict[str, Any], float]]] = defaultdict(list)
    for m in markets:
        key = _group_key(m)
        if not key:
            continue
        if not _volume_ok(m):
            continue
        prices = _extract_outcome_prices(m)
        if not _is_binary(prices):
            continue
        th = _extract_threshold(m.get("question") or "")
        if th is None:
            continue
        yes = prices[0]
        groups[key].append((th, m, yes))

    opps = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        # Ordena ascendente por threshold; P(>=X) debe ser >= P(>=Y) si X<Y.
        items.sort(key=lambda t: t[0])
        for i in range(len(items) - 1):
            th_lo, m_lo, p_lo = items[i]
            th_hi, m_hi, p_hi = items[i + 1]
            # Violacion: threshold mayor tiene precio estrictamente mayor.
            if p_hi > p_lo + MONOTONIC_MIN_DIFF:
                opps.append({
                    "arb_type": "monotonic_violation",
                    "group_key": key,
                    "lower_threshold": th_lo,
                    "higher_threshold": th_hi,
                    "lower_question": m_lo.get("question"),
                    "higher_question": m_hi.get("question"),
                    "lower_price": round(p_lo, 4),
                    "higher_price": round(p_hi, 4),
                    "edge_pct": round((p_hi - p_lo) * 100, 3),
                    "action": "sell_higher_buy_lower",
                    "lower_market_id": m_lo.get("id"),
                    "higher_market_id": m_hi.get("id"),
                })
    return opps


# ---------------------------------------------------------------------------
# Persistencia a Base44 (Opportunity entity)
# ---------------------------------------------------------------------------
def _persist_opportunity(opp: Dict[str, Any]) -> None:
    if not BASE44_API_KEY:
        return
    url = "%s/api/apps/%s/entities/Opportunity" % (BASE44_BASE_URL, BASE44_APP_ID)
    payload = {
        "market_title": (
            opp.get("question") or opp.get("umbrella_question")
            or opp.get("higher_question") or opp.get("group_key") or "unknown"
        ),
        "spread_pct": float(opp.get("edge_pct") or 0.0) / 100.0,
        "profit_per_100": float(opp.get("profit_per_100")
                                or opp.get("edge_pct") or 0.0),
        "arb_type": opp.get("arb_type"),
        "status": "detected",
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        resp = requests.post(url, json=payload,
                             headers={"api_key": BASE44_API_KEY,
                                      "Content-Type": "application/json"},
                             timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Opportunity persist %d: %s",
                           resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        logger.warning("Opportunity persist fallo: %s", exc)


# ---------------------------------------------------------------------------
# Entry point publico
# ---------------------------------------------------------------------------
def scan_logical_arb() -> List[Dict[str, Any]]:
    """Ejecuta las 3 familias de deteccion y devuelve top-N por edge_pct.

    Persiste cada deteccion como Opportunity en Base44 para analisis.
    """
    markets = _fetch_markets()
    if not markets:
        return []

    binary = _scan_binary_under(markets)
    umbrella = _scan_umbrella(markets)
    monotonic = _scan_monotonic(markets)

    all_opps = binary + umbrella + monotonic
    all_opps.sort(key=lambda o: float(o.get("edge_pct") or 0.0), reverse=True)
    top = all_opps[:TOP_N]

    logger.info(
        "logical_arb: binary=%d umbrella=%d monotonic=%d (top %d reportados)",
        len(binary), len(umbrella), len(monotonic), len(top),
    )
    for opp in top[:5]:  # persiste solo los 5 mejores para no saturar
        _persist_opportunity(opp)

    return top
