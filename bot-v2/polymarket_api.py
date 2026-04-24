"""polymarket_api.py - Helpers reutilizables del CLOB de Polymarket.

Este modulo NO tiene estado. Son funciones puras que envuelven endpoints
publicos/privados de Polymarket con manejo de errores consistente.

Diseno:
  - Todas las funciones devuelven (value, error) o None si falla.
  - Ninguna lanza excepcion: el caller decide que hacer con error.
  - Timeouts explicitos para no colgar el bot si la API esta lenta.
"""

import logging
import math
import time
from typing import Optional, Tuple, Dict, Any

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
DEFAULT_TIMEOUT = 8.0

# Cache de tick size por token_id (TTL 1h). Raramente cambia.
_TICK_CACHE: Dict[str, Tuple[float, float]] = {}  # token_id -> (tick, expires_at)
_TICK_TTL = 3600.0


def get_tick_size(token_id: str) -> float:
    """Devuelve el minimum tick (price increment) para un token.

    Polymarket devuelve tick como fraction (ej 0.01, 0.001).
    Si la API falla, default conservador 0.01 (centavo).

    Cacheado 1h porque cambia muy poco.
    """
    if not token_id:
        return 0.01

    now = time.time()
    cached = _TICK_CACHE.get(token_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        r = requests.get(
            f"{CLOB_BASE}/tick-size",
            params={"token_id": token_id},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code == 200:
            data = r.json() or {}
            tick = float(data.get("minimum_tick_size") or data.get("tick_size") or 0.01)
            if tick > 0:
                _TICK_CACHE[token_id] = (tick, now + _TICK_TTL)
                return tick
    except Exception as exc:
        logger.debug("get_tick_size fallo token=%s: %s", token_id[:10], exc)

    return 0.01


def round_price_to_tick(price: float, tick: float) -> float:
    """Redondea un precio al tick valido mas cercano (clamped 0.01 .. 0.99)."""
    if tick <= 0:
        tick = 0.01
    price = max(0.01, min(0.99, float(price)))
    # Round to nearest multiple of tick
    steps = round(price / tick)
    rounded = steps * tick
    # Evitar errores de float (ej 0.30000000000000004)
    decimals = max(2, abs(int(math.log10(tick))) if tick < 1 else 2)
    return round(rounded, decimals)


def get_order_book(token_id: str) -> Optional[Dict[str, Any]]:
    """Fetcha el orderbook publico. Devuelve dict con 'bids' y 'asks' o None."""
    if not token_id:
        return None
    try:
        r = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            logger.debug("get_order_book HTTP %d token=%s", r.status_code, token_id[:10])
            return None
        return r.json() or {}
    except Exception as exc:
        logger.debug("get_order_book fallo token=%s: %s", token_id[:10], exc)
        return None


def best_bid_ask(token_id: str) -> Tuple[Optional[float], Optional[float]]:
    """Devuelve (best_bid, best_ask) o (None, None) si no hay libro."""
    book = get_order_book(token_id)
    if not book:
        return (None, None)
    bids = book.get("bids") or []
    asks = book.get("asks") or []

    def _pick(levels, reverse):
        parsed = []
        for lvl in levels:
            try:
                p = float(lvl.get("price", 0))
                s = float(lvl.get("size", 0))
                if p > 0 and s > 0:
                    parsed.append(p)
            except (TypeError, ValueError):
                continue
        if not parsed:
            return None
        parsed.sort(reverse=reverse)
        return parsed[0]

    return (_pick(bids, True), _pick(asks, False))


def compute_order_size(size_usdc: float, price: float, min_notional: float = 5.0) -> Optional[float]:
    """COMPUTE_ORDER_SIZE_V2: calcula shares para que notional respete reglas CLOB.

    Polymarket exige que `maker_amount` (shares*price para BUY) se pueda expresar
    en USDC con <= 6 decimales (USDC es 1e6). En la practica el CLOB valida a 4dp.

    Estrategia:
      1. Partimos de floor(size_usdc/price) en pasos de 0.01 shares.
      2. Escaneamos hasta 101 iteraciones (~ $1 de rango) buscando el mayor
         size_shares tal que shares*price sea limpio a 4 decimales.
      3. Si ni eso encontramos, devolvemos None (orden se rechazara arriba).

    Devuelve size_shares (>= min_notional/price) o None.
    """
    if price <= 0 or size_usdc <= 0:
        return None

    # Presupuesto exacto a 2dp (floor para no exceder)
    budget = math.floor(size_usdc * 100) / 100.0
    if budget < min_notional:
        return None

    raw_shares = budget / price
    size_shares = math.floor(raw_shares * 100) / 100.0

    # Escaneamos bajando de a 0.01 share hasta encontrar notional limpio a 4dp.
    # 101 iteraciones = $1 de rango, suficiente para cualquier price realista.
    best = None
    for _ in range(101):
        if size_shares <= 0:
            break
        notional_real = size_shares * price
        # Limpio a 4 decimales (lo que exige el CLOB, no 2)
        if abs(notional_real * 10000 - round(notional_real * 10000)) < 1e-6:
            best = size_shares
            break
        size_shares = round(size_shares - 0.01, 2)

    if best is None or best <= 0 or best * price < min_notional:
        return None

    return round(best, 2)


def classify_error(exc: Exception) -> str:
    """Clasifica una excepcion de la API CLOB.

    Devuelve:
      - "transient"  -> worth retry (timeout, 5xx, connection error)
      - "rejected"   -> rechazo explicito del CLOB (400, invalid params)
      - "auth"       -> problema de credenciales/signature
      - "unknown"    -> no podemos decidir -> tratar como transient
    """
    msg = str(exc).lower()
    if "timeout" in msg or "timed out" in msg:
        return "transient"
    if "connection" in msg or "connect" in msg:
        return "transient"
    if "5" in msg and "status_code=5" in msg:
        return "transient"
    if "400" in msg or "invalid" in msg or "rejected" in msg:
        return "rejected"
    if "401" in msg or "403" in msg or "unauthorized" in msg or "signature" in msg:
        return "auth"
    return "unknown"


def check_usdc_balance(client, funder: str) -> Optional[float]:
    """Chequea balance USDC disponible en la Safe via CLOB client.

    Devuelve USDC available o None si no podemos leer.
    """
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        resp = client.get_balance_allowance(params) or {}
        # Polymarket devuelve balance en raw units (6 decimales para USDC)
        raw = float(resp.get("balance", 0) or 0)
        return raw / 1_000_000.0
    except Exception as exc:
        logger.debug("check_usdc_balance fallo: %s", exc)
        return None
