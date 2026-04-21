"""auto_close.py - Cierra automaticamente Position abiertas por TP/SL.

Logica cada N iteraciones:
  1. Lee BotConfig (take_profit, stop_loss) via bot_config_reader.
  2. Lista Position con status='open' desde Base44.
  3. Para cada una obtiene el midpoint actual del CLOB publico.
  4. Calcula pnl_pct segun side.
  5. Si cruza TP o SL -> intenta cerrar.

CIERRE:
  - Paper (DRY_RUN): actualiza Base44 + crea Trade.
  - Live: intenta om.close_position_market. SOLO crea Trade si el cierre
    real fue exitoso. Si falla, deja la Position abierta para reintentar.
    Despues de MAX_FAIL_ATTEMPTS fallos, marca la Position como closed
    con close_reason='unverified_no_trade' SIN crear Trade record.

No toca posiciones sin token_id o sin entry_price > 0.
"""

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
    buy = _fetch_side(token_id, "BUY")
    sell = _fetch_side(token_id, "SELL")
    if buy and sell and buy > 0 and sell > 0:
        return (buy + sell) / 2.0
    return buy or sell


def _compute_pnl_pct(side, entry, current):
    if entry is None or entry <= 0 or current is None:
        return None
    if (side or "BUY").upper() == "SELL":
        return (entry - current) / entry
    return (current - entry) / entry


def _compute_pnl_usdc(side, entry, current, size_tokens):
    if (side or "BUY").upper() == "SELL":
        return (entry - current) * size_tokens
    return (current - entry) * size_tokens


class AutoClose:
    """Cierra Position abiertas cuando cruzan TP o SL."""

    def __init__(self, order_manager=None):
        self.om = order_manager
        # Track de fallos por posicion (en memoria). Se pierde al reiniciar
        # el bot, pero eso esta OK porque evita bucles eternos de retry.
        self._fail_counts = {}

    def _try_live_close(self, pos, size_tokens):
        """Retorna True si el cierre REAL fue exitoso, False si fallo.
        En modo paper retorna True siempre (simulado)."""
        if DRY_RUN:
            return True
        if self.om is None:
            return False
        token_id = pos.get("token_id")
        side = (pos.get("side") or "BUY").upper()
        close_side = "SELL" if side == "BUY" else "BUY"
        try:
            fn = getattr(self.om, "close_position_market", None)
            if not callable(fn):
                return False
            result = fn(
                token_id=token_id,
                size=size_tokens,
                side=close_side,
                market_id=pos.get("market"),
                strategy=pos.get("strategy"),
            )
            # close_position_market puede retornar dict/obj con 'success'
            # o None. Si es None asumimos fallo (no confirmado).
            if result is None:
                return False
            if isinstance(result, dict):
                return bool(result.get("success", False))
            # Fallback: si no nos dice explicitamente, asumimos exito.
            return True
        except Exception as exc:
            logger.warning("close live fallo (%s): %s",
                           str(pos.get("id"))[:8], exc)
            return False

    def _mark_unverified_close(self, pos):
        """Cuando el cierre real fallo N veces, sacamos la posicion del
        loop pero SIN registrar Trade (porque no sabemos PnL real)."""
        pos_id = pos.get("id")
        update_record("Position", pos_id, {
            "status": "closed",
            "close_time": _iso_now(),
            "close_reason": "unverified_no_trade",
            "pnl_unrealized": 0.0,
        })
        logger.warning(
            "AutoClose: pos=%s marcada closed sin Trade (live close fallo %dx)",
            str(pos_id)[:8], MAX_FAIL_ATTEMPTS,
        )

    def _finalize_close(self, pos, current_price, pnl_usdc, pnl_pct, reason):
        """Marca Position closed Y crea Trade. Solo se llama cuando el cierre
        real fue exitoso (live) o en paper mode."""
        pos_id = pos.get("id")
        entry = _safe_float(pos.get("entry_price"))
        size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
        now_iso = _iso_now()

        update_record("Position", pos_id, {
            "status": "closed",
            "current_price": round(current_price, 4),
            "pnl_unrealized": 0.0,
            "pnl_realized": round(pnl_usdc, 4),
            "close_time": now_iso,
            "close_reason": reason,
        })

        create_record("Trade", {
            "market": pos.get("market"),
            "side": pos.get("side"),
            "entry_price": entry,
            "exit_price": round(current_price, 4),
            "size_usdc": size_usdc,
            "pnl": round(pnl_usdc, 4),
            "pnl_pct": round(pnl_pct * 100.0, 3),
            "strategy": pos.get("strategy"),
            "status": "closed",
            "entry_time": pos.get("opened_at") or pos.get("created_date"),
            "exit_time": now_iso,
            "notes": "auto_close:%s" % reason,
        })

        logger.info(
            "AutoClose %s %s pos=%s entry=%.4f exit=%.4f pnl=%+.2f (%.2f%%)",
            reason.upper(), "[PAPER]" if DRY_RUN else "[LIVE]",
            str(pos_id)[:8], entry or 0.0, current_price, pnl_usdc,
            pnl_pct * 100.0,
        )

    def _close_position(self, pos, current_price, pnl_usdc, pnl_pct, reason):
        pos_id = pos.get("id")
        entry = _safe_float(pos.get("entry_price"))
        size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
        size_tokens = _safe_float(pos.get("size_tokens"))
        if (size_tokens is None or size_tokens <= 0) and entry and entry > 0:
            size_tokens = size_usdc / entry

        ok = self._try_live_close(pos, size_tokens or 0.0)

        if ok:
            # Cierre real OK (o paper): registrar normal
            self._fail_counts.pop(pos_id, None)
            self._finalize_close(pos, current_price, pnl_usdc, pnl_pct, reason)
            return

        # Cierre real FALLO. No creamos Trade. Contamos fallos.
        fails = self._fail_counts.get(pos_id, 0) + 1
        self._fail_counts[pos_id] = fails
        logger.warning(
            "AutoClose: pos=%s cierre live fallo (%d/%d). No se crea Trade.",
            str(pos_id)[:8], fails, MAX_FAIL_ATTEMPTS,
        )
        if fails >= MAX_FAIL_ATTEMPTS:
            self._mark_unverified_close(pos)
            self._fail_counts.pop(pos_id, None)

    def run(self):
        cfg = fetch_bot_config() or {}
        tp = _safe_float(cfg.get("take_profit"))
        sl = _safe_float(cfg.get("stop_loss"))
        if tp is None and sl is None:
            logger.debug("AutoClose: sin TP/SL configurados, skip.")
            return 0

        positions = list_records("Position", sort="-updated_date", limit=200) or []
        closed = 0
        checked = 0

        for pos in positions:
            if pos.get("status") != "open":
                continue
            if not pos.get("id"):
                continue
            entry = _safe_float(pos.get("entry_price"))
            if entry is None or entry <= 0:
                continue
            token_id = pos.get("token_id")
            if not token_id:
                continue

            current = _fetch_midpoint(token_id)
            if current is None or current <= 0:
                continue

            checked += 1
            pnl_pct = _compute_pnl_pct(pos.get("side"), entry, current)
            if pnl_pct is None:
                continue

            reason = None
            if tp is not None and pnl_pct >= tp:
                reason = "take_profit"
            elif sl is not None and pnl_pct <= sl:
                reason = "stop_loss"
            if reason is None:
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0
            pnl_usdc = _compute_pnl_usdc(pos.get("side"), entry, current, size_tokens)

            self._close_position(pos, current, pnl_usdc, pnl_pct, reason)
            closed += 1

        logger.info(
            "AutoClose: tp=%s sl=%s checked=%d closed=%d mode=%s",
            tp, sl, checked, closed, "paper" if DRY_RUN else "live",
        )
        return closed
