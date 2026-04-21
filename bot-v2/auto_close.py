"""auto_close.py - Cierra automaticamente Position abiertas por TP/SL.

v2025-04-21c: fix final del kwarg TypeError.
  - Llama a om.close_position_market introspectivamente: detecta si la
    signature tiene 'shares', 'size' o nada, y pasa el arg correcto.
  - Si el cierre live falla 3 veces -> NO crea Trade (no ghost trades).
"""

import inspect
import logging
from datetime import datetime, timezone

import requests

from base44_client import create_record, list_records, update_record
from bot_config_reader import fetch_bot_config
from config import DRY_RUN

logger = logging.getLogger(__name__)

CLOB_PRICE_URL = "https://clob.polymarket.com/price"
REQUEST_TIMEOUT = 8
MAX_FAIL_ATTEMPTS = 3
_FAIL_COUNTS = {}


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_side(token_id, side):
    try:
        resp = requests.get(
            CLOB_PRICE_URL,
            params={"token_id": token_id, "side": side},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        return _safe_float(resp.json().get("price"))
    except (requests.RequestException, ValueError):
        return None


def _fetch_midpoint(token_id):
    if not token_id:
        return None
    bid = _fetch_side(token_id, "buy")
    ask = _fetch_side(token_id, "sell")
    if bid is None or ask is None or ask <= bid:
        return None
    return (bid + ask) / 2.0


def _compute_pnl_pct(pos, current_price):
    entry = _safe_float(pos.get("entry_price"))
    if not entry or entry <= 0 or current_price is None:
        return None
    if pos.get("side") == "BUY":
        return (current_price - entry) / entry
    return (entry - current_price) / entry


def _call_close_market(om, pos):
    """Llama a om.close_position_market introspectivamente.
    
    Maneja las tres variantes historicas de la signature:
      - close_position_market(position_id, shares=N)
      - close_position_market(position_id, size=N)  
      - close_position_market(position_id)  (sin cantidad, toma toda la pos)
    """
    pos_id = pos.get("id")
    shares = _safe_float(pos.get("size_tokens")) or _safe_float(pos.get("size_usdc"))
    
    try:
        sig = inspect.signature(om.close_position_market)
        params = sig.parameters
    except (ValueError, TypeError):
        # Fallback: sin introspeccion, intentar sin kwarg de cantidad.
        return om.close_position_market(pos_id)
    
    # Construir kwargs segun lo que acepta la funcion
    kwargs = {}
    if "shares" in params and shares is not None:
        kwargs["shares"] = shares
    elif "size" in params and shares is not None:
        kwargs["size"] = shares
    # Si no tiene ninguno, simplemente no pasamos cantidad
    
    return om.close_position_market(pos_id, **kwargs)


def _close_position(om, pos, pnl_pct, reason, current_price):
    pos_id = pos.get("id")
    if not pos_id:
        return False

    # --- Paper mode ---
    if DRY_RUN or not om:
        update_record("Position", pos_id, {
            "status": "closed",
            "close_time": _iso_now(),
            "close_reason": reason,
            "current_price": current_price,
            "pnl_unrealized": 0,
        })
        entry = _safe_float(pos.get("entry_price")) or 0
        size = _safe_float(pos.get("size_usdc")) or 0
        pnl = size * pnl_pct if pnl_pct else 0
        create_record("Trade", {
            "market": pos.get("market"),
            "side": pos.get("side"),
            "entry_price": entry,
            "exit_price": current_price,
            "size_usdc": size,
            "pnl": pnl,
            "pnl_pct": (pnl_pct or 0) * 100,
            "strategy": pos.get("strategy") or "unknown",
            "status": "closed",
            "entry_time": pos.get("opened_at"),
            "exit_time": _iso_now(),
            "notes": f"auto_close paper {reason}",
        })
        _FAIL_COUNTS.pop(pos_id, None)
        return True

    # --- Live mode: intentar cierre real ---
    try:
        result = _call_close_market(om, pos)
        ok = bool(result) if result is not None else True
    except Exception as exc:
        logger.warning("close live fallo (%s): %s", pos_id[-8:], exc)
        ok = False

    if ok:
        # Cierre real exitoso -> marcar closed + crear Trade
        update_record("Position", pos_id, {
            "status": "closed",
            "close_time": _iso_now(),
            "close_reason": reason,
            "current_price": current_price,
            "pnl_unrealized": 0,
        })
        entry = _safe_float(pos.get("entry_price")) or 0
        size = _safe_float(pos.get("size_usdc")) or 0
        pnl = size * pnl_pct if pnl_pct else 0
        create_record("Trade", {
            "market": pos.get("market"),
            "side": pos.get("side"),
            "entry_price": entry,
            "exit_price": current_price,
            "size_usdc": size,
            "pnl": pnl,
            "pnl_pct": (pnl_pct or 0) * 100,
            "strategy": pos.get("strategy") or "unknown",
            "status": "closed",
            "entry_time": pos.get("opened_at"),
            "exit_time": _iso_now(),
            "notes": f"auto_close live {reason}",
        })
        _FAIL_COUNTS.pop(pos_id, None)
        return True

    # Cierre live fallo -> incrementar contador, NO crear Trade
    _FAIL_COUNTS[pos_id] = _FAIL_COUNTS.get(pos_id, 0) + 1
    attempts = _FAIL_COUNTS[pos_id]
    logger.warning("AutoClose: pos=%s cierre live fallo (%d/%d). No se crea Trade.",
                   pos_id[-8:], attempts, MAX_FAIL_ATTEMPTS)

    if attempts >= MAX_FAIL_ATTEMPTS:
        # Demasiados fallos -> marcar closed con reason especial SIN Trade
        update_record("Position", pos_id, {
            "status": "closed",
            "close_time": _iso_now(),
            "close_reason": "unverified_no_trade",
            "current_price": current_price,
        })
        logger.warning("AutoClose: pos=%s marcada closed sin Trade (live close fallo %dx)",
                       pos_id[-8:], MAX_FAIL_ATTEMPTS)
        _FAIL_COUNTS.pop(pos_id, None)
    return False


def check_and_close(om=None):
    cfg = fetch_bot_config() or {}
    take_profit = _safe_float(cfg.get("take_profit")) or 0.05
    stop_loss = _safe_float(cfg.get("stop_loss")) or -0.025

    positions = list_records("Position", {"status": "open"}, limit=100)
    if not positions:
        return

    checked = 0
    closed = 0
    for pos in positions:
        token_id = pos.get("token_id")
        entry = _safe_float(pos.get("entry_price"))
        if not token_id or not entry or entry <= 0:
            continue
        if pos.get("pending_fill"):
            continue

        current = _fetch_midpoint(token_id)
        if current is None:
            continue

        # Phantom guard: descartar PnL irreal
        pnl_pct = _compute_pnl_pct(pos, current)
        if pnl_pct is None or pnl_pct > 5.0 or pnl_pct < -0.95:
            continue

        checked += 1
        reason = None
        if pnl_pct >= take_profit:
            reason = "take_profit"
        elif pnl_pct <= stop_loss:
            reason = "stop_loss"

        if reason:
            if _close_position(om, pos, pnl_pct, reason, current):
                closed += 1

    mode = "paper" if DRY_RUN else "live"
    logger.info("AutoClose: tp=%s sl=%s checked=%d closed=%d mode=%s",
                take_profit, stop_loss, checked, closed, mode)
