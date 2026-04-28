"""logical_arb.py - Deteccion de arbitrajes logicos en Polymarket (v2.0).

v2.0 (Fase 1.4): Binary Under ahora se valida contra el orderbook CLOB real
antes de reportar. Gamma outcomePrices es un midpoint y no es cruzable con
ordenes limit. El executor necesita best_ask real + liquidez disponible.

Flujo Binary Under:
  1) Pre-filtro barato con Gamma: volumen, liquidez, YES+NO<threshold.
  2) Por cada candidato, fetch del orderbook CLOB para YES y NO.
  3) Valida que best_ask(YES) + best_ask(NO) sigue siendo arbitrable
     despues de consultar el book real (los precios Gamma estan stale).
  4) Enriquece la oportunidad con token_ids y sizes disponibles.

Umbrella y Monotonic: siguen siendo solo deteccion informativa.
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
CLOB_BASE_URL = "https://clob-v2.polymarket.com"

# Umbrales Binary Under
BINARY_PREFILTER_THRESHOLD = 0.98     # Gamma: aceptamos hasta 0.98 para no perder candidatos
BINARY_CLOB_MIN_EDGE = 0.025          # CLOB real: al menos 2.5% edge neto (1-asks)
BINARY_MIN_ASK_SIZE_SHARES = 20.0     # shares disponibles al tope; <20 es ilquido

# Umbrales otros
UMBRELLA_MIN_DIFF = 0.03
MONOTONIC_MIN_DIFF = 0.02

# Filtros volumen/liquidez
MIN_VOLUME_USDC = 10_000.0
MIN_LIQUIDITY_USDC = 1_000.0
TOP_N = 20
MAX_CLOB_LOOKUPS_PER_SCAN = 30        # rate limit safety


# ---------------------------------------------------------------------------
# Helpers Gamma
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
# CLOB orderbook lookup (solo para binary under)
# ---------------------------------------------------------------------------
def _fetch_clob_book(token_id: str) -> Optional[Dict[str, Any]]:
    """GET /book?token_id=... devuelve {bids, asks, market, ...}. Orderbook
    publico, sin auth. Devuelve None si falla.
    """
    try:
        resp = requests.get(
            "%s/book" % CLOB_BASE_URL,
            params={"token_id": token_id},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict):
            return None
        return data
    except requests.RequestException as exc:
        logger.debug("CLOB book fetch fallo token=%s: %s", token_id, exc)
        return None


def _best_ask(book: Dict[str, Any]) -> Tuple[Optional[float], float]:
    """Devuelve (price, size) del mejor ask. CLOB ordena asks ascendente
    por precio, pero siempre sort defensivo.
    """
    asks = book.get("asks") or []
    if not asks:
        return None, 0.0
    try:
        # Cada nivel es {"price": str, "size": str}
        parsed = [(float(a.get("price", 0)), float(a.get("size", 0))) for a in asks]
        parsed = [p for p in parsed if p[0] > 0 and p[1] > 0]
        if not parsed:
            return None, 0.0
        parsed.sort(key=lambda x: x[0])
        price, size = parsed[0]
        return price, size
    except (TypeError, ValueError):
        return None, 0.0


# ---------------------------------------------------------------------------
# 1) BINARY_UNDER con enriquecimiento CLOB
# ---------------------------------------------------------------------------
def _scan_binary_under(markets) -> List[Dict[str, Any]]:
    # Paso A: pre-filtrado barato con Gamma
    candidates = []
    for m in markets:
        if not _volume_ok(m):
            continue
        prices = _extract_outcome_prices(m)
        if not _is_binary(prices):
            continue
        yes, no = prices[0], prices[1]
        gamma_total = yes + no
        if gamma_total >= BINARY_PREFILTER_THRESHOLD or gamma_total <= 0:
            continue
        token_ids = _extract_token_ids(m)
        if len(token_ids) < 2:
            continue
        candidates.append((m, yes, no, token_ids))

    # Ordena por edge "Gamma" descendente y limita consultas CLOB
    candidates.sort(key=lambda c: 1.0 - (c[1] + c[2]), reverse=True)
    candidates = candidates[:MAX_CLOB_LOOKUPS_PER_SCAN]

    opps = []
    for market, gamma_yes, gamma_no, token_ids in candidates:
        token_yes, token_no = token_ids[0], token_ids[1]

        book_yes = _fetch_clob_book(token_yes)
        book_no = _fetch_clob_book(token_no)
        if not book_yes or not book_no:
            continue

        ask_yes_price, ask_yes_size = _best_ask(book_yes)
        ask_no_price, ask_no_size = _best_ask(book_no)
        if ask_yes_price is None or ask_no_price is None:
            continue
        if ask_yes_size < BINARY_MIN_ASK_SIZE_SHARES or ask_no_size < BINARY_MIN_ASK_SIZE_SHARES:
            continue

        clob_total = ask_yes_price + ask_no_price
        edge = 1.0 - clob_total
        if edge < BINARY_CLOB_MIN_EDGE:
            continue

        # Liquidez maxima arbitrable al tope del book
        max_shares = min(ask_yes_size, ask_no_size)
        max_notional_usdc = max_shares * clob_total  # USDC necesarios

        opps.append({
            "arb_type": "binary_under",
            "market_id": market.get("id") or market.get("conditionId"),
            "question": market.get("question") or market.get("slug"),
            "token_id_yes": token_yes,
            "token_id_no": token_no,
            "ask_yes": round(ask_yes_price, 4),
            "ask_no": round(ask_no_price, 4),
            "ask_size_yes": round(ask_yes_size, 2),
            "ask_size_no": round(ask_no_size, 2),
            "max_arb_shares": round(max_shares, 2),
            "max_arb_notional_usdc": round(max_notional_usdc, 2),
            "total": round(clob_total, 4),
            "edge_pct": round(edge * 100, 3),
            "profit_per_100": round(edge / clob_total * 100, 3),
            "gamma_yes": round(gamma_yes, 4),
            "gamma_no": round(gamma_no, 4),
            "volume": _safe_float(market.get("volume") or market.get("volumeNum")),
        })
    return opps


# ---------------------------------------------------------------------------
# 2) UMBRELLA (sin enriquecimiento, solo deteccion)
# ---------------------------------------------------------------------------
UMBRELLA_PATTERNS = re.compile(
    r"(any\s+\w+\s+win|will\s+any|any\s+other|field|other\s+candidate)",
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
            continue

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
# 3) MONOTONIC (sin cambios, solo deteccion)
# ---------------------------------------------------------------------------
THRESHOLD_PATTERN = re.compile(r"\$?([\d,]+(?:\.\d+)?)\s*(k|m|bn)?", re.IGNORECASE)


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
        items.sort(key=lambda t: t[0])
        for i in range(len(items) - 1):
            th_lo, m_lo, p_lo = items[i]
            th_hi, m_hi, p_hi = items[i + 1]
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

    Binary Under incluye token_ids y precios CLOB reales listos para executor.
    Umbrella y Monotonic son solo informativos.
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
    for opp in top[:5]:
        _persist_opportunity(opp)

    return top
