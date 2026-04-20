"""auto_close.py - Cierra automaticamente Position abiertas por TP/SL.

Logica cada N iteraciones:
  1. Lee BotConfig (take_profit, stop_loss) via bot_config_reader.
  2. Lista Position con status='open' desde Base44.
  3. Para cada una obtiene el midpoint actual del CLOB publico.
  4. Calcula pnl_pct = (current - entry) / entry  (BUY) o
                      (entry - current) / entry   (SELL).
  5. Si pnl_pct >= take_profit  -> cierra por take_profit.
     Si pnl_pct <= stop_loss    -> cierra por stop_loss.
  6. Al cerrar:
     - paper (DRY_RUN): solo actualiza Base44.
     - live: intenta om.close_position_market(token_id, size_tokens, ...)
       en best-effort; sea cual sea el resultado, marca la position como
       closed y crea un Trade closed con pnl/pnl_pct realizados.

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
MAX_CLOSES_PER_RUN = 10


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
        # En paper mode se deja en None; en live se pasa OrderManager para
        # intentar el cierre real en el CLOB.
        self.om = order_manager

    def _try_live_close(self, pos, size_tokens):
        """Best-effort: llama al CLOB real. No rompe el flujo si falla."""
        if DRY_RUN or self.om is None:
            return False
        token_id = pos.get("token_id")
        side = (pos.get("side") or "BUY").upper()
        close_side = "SELL" if side == "BUY" else "BUY"
        try:
            fn = getattr(self.om, "close_position_market", None)
            if callable(fn):
                fn(
                    token_id=token_id,
                    size=size_tokens,
                    side=close_side,
                    market_id=pos.get("market"),
                    strategy=pos.get("strategy"),
                )
                return True
        except Exception as exc:
            logger.warning("close live fallo (%s): %s",
                           str(pos.get("id"))[:8], exc)
        return False

    def _close_position(self, pos, current_price, pnl_usdc, pnl_pct, reason):
        pos_id = pos.get("id")
        entry = _safe_float(pos.get("entry_price"))
        size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
        size_tokens = _safe_float(pos.get("size_tokens"))
        if (size_tokens is None or size_tokens <= 0) and entry and entry > 0:
            size_tokens = size_usdc / entry

        # 1) Intento de cierre real (live). En paper se skip.
        self._try_live_close(pos, size_tokens or 0.0)

        # 2) Marcar Position como closed.
        now_iso = _iso_now()
        update_record("Position", pos_id, {
            "status": "closed",
            "current_price": round(current_price, 4),
            "pnl_unrealized": 0.0,
            "pnl_realized": round(pnl_usdc, 4),
            "close_time": now_iso,
            "close_reason": reason,
        })

        # 3) Crear Trade closed con PnL realizado.
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

    def run(self):
        logger.info("AutoClose: run() invocado.")
        cfg = fetch_bot_config() or {}
        tp = _safe_float(cfg.get("take_profit"))
        sl = _safe_float(cfg.get("stop_loss"))
        if tp is None and sl is None:
            logger.info("AutoClose: sin TP/SL en BotConfig, skip.")
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
            logger.info(
                "AutoClose check pos=%s %s entry=%.4f cur=%.4f pnl_pct=%+.2f%% tp=%s sl=%s reason=%s",
                str(pos.get("id"))[:8], pos.get("side"), entry, current,
                pnl_pct * 100.0, tp, sl, reason,
            )
            if reason is None:
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0
            pnl_usdc = _compute_pnl_usdc(pos.get("side"), entry, current, size_tokens)

            self._close_position(pos, current, pnl_usdc, pnl_pct, reason)
            closed += 1
            if closed >= MAX_CLOSES_PER_RUN:
                logger.info(
                    "AutoClose: alcanzado MAX_CLOSES_PER_RUN=%d, resto se cerrara en el siguiente ciclo.",
                    MAX_CLOSES_PER_RUN,
                )
                break

        logger.info(
            "AutoClose: tp=%s sl=%s checked=%d closed=%d mode=%s",
            tp, sl, checked, closed, "paper" if DRY_RUN else "live",
        )
        return closed
