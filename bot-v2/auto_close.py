"""auto_close.py - Cierra automaticamente Position abiertas por TP/SL/Trailing.

Marker: TRAILING_STOP_V1

Logica cada N iteraciones:
  1. Lee BotConfig (take_profit, stop_loss, trailing_*) via bot_config_reader.
  2. Lista Position con status='open' desde Base44.
  3. Para cada una obtiene el midpoint actual del CLOB publico.
  4. Calcula pnl_pct.
  5. Decision (en orden de prioridad):
     - TP fijo:  pnl_pct >= take_profit -> close (take_profit).
     - Trailing: si trailing_stop_enabled y el peak ya activo:
                 pnl_pct <= peak - trailing_distance -> close (trailing_stop).
     - SL fijo:  pnl_pct <= stop_loss -> close (stop_loss).
  6. Si trailing activo y peak avanzo, persiste pnl_peak_pct en Position.

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
    """Cierra Position abiertas cuando cruzan TP/SL/Trailing."""

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
                    shares=size_tokens,
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
        # VERIFY_SELL_BEFORE_CLOSE_V1: en modo LIVE solo marcamos closed si el sell se confirmo.
        sell_ok = self._try_live_close(pos, size_tokens or 0.0)
        if not DRY_RUN and not sell_ok:
            logger.warning(
                "AutoClose SELL no confirmado, posicion %s sigue OPEN (reason=%s pnl=%+.2f)",
                str(pos_id)[:8], reason, pnl_usdc,
            )
            # No cerramos: la posicion queda open y el loop reintentara
            # en el proximo ciclo.
            return

        # 2) Marcar Position como closed (solo si sell confirmado o paper).
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

    def _update_peak(self, pos_id, new_peak):
        """Persiste pnl_peak_pct en Position. Best-effort."""
        try:
            update_record("Position", pos_id, {
                "pnl_peak_pct": round(new_peak, 5),
            })
        except Exception as exc:
            logger.debug("no pude persistir peak en %s: %s",
                         str(pos_id)[:8], exc)

    def run(self):
        cfg = fetch_bot_config() or {}
        tp = _safe_float(cfg.get("take_profit"))
        sl = _safe_float(cfg.get("stop_loss"))
        trail_on = bool(cfg.get("trailing_stop_enabled"))
        trail_act = _safe_float(cfg.get("trailing_activation_pct")) or 0.10
        trail_dist = _safe_float(cfg.get("trailing_distance_pct")) or 0.05

        if tp is None and sl is None and not trail_on:
            logger.debug("AutoClose: sin TP/SL/Trailing configurados, skip.")
            return 0

        positions = list_records("Position", sort="-updated_date", limit=200) or []
        closed = 0
        checked = 0

        for pos in positions:
            if pos.get("status") != "open":
                continue
            if pos.get("pending_fill"):
                # Orden aun no rellenada en el CLOB: no es una posicion real.
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

            # --- TRAILING: actualizar peak si corresponde ---
            prev_peak = _safe_float(pos.get("pnl_peak_pct"))
            new_peak = prev_peak if prev_peak is not None else pnl_pct
            if pnl_pct > new_peak:
                new_peak = pnl_pct
                if trail_on and pnl_pct >= trail_act:
                    # Solo persistimos si trailing esta activo para no
                    # escribir writes innecesarios a Base44.
                    self._update_peak(pos.get("id"), new_peak)

            # --- DECISION DE CIERRE (orden de prioridad) ---
            reason = None

            # 1) TP fijo (siempre, techo absoluto)
            if tp is not None and pnl_pct >= tp:
                reason = "take_profit"

            # 2) Trailing stop (solo si activo y ya pasamos activation)
            elif trail_on and new_peak >= trail_act:
                trigger = new_peak - trail_dist
                if pnl_pct <= trigger:
                    reason = "trailing_stop"

            # 3) SL fijo (solo si trailing no disparo)
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
            "AutoClose: tp=%s sl=%s trail=%s checked=%d closed=%d mode=%s",
            tp, sl, "on" if trail_on else "off", checked, closed,
            "paper" if DRY_RUN else "live",
        )
        return closed
