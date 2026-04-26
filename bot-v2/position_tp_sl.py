"""
position_tp_sl.py — TP/SL loop con fallback para Positions whale_consensus.

Por que existe:
  Los cierres dust_unsellable (Merida, Moutet) ocurrian porque Polymarket
  exige minimo 5 shares por orden. Cuando el bot intentaba cerrar una
  posicion de $9-$30 a precios bajos, las shares restantes caian bajo 5,
  el CLOB rechazaba y el bot dejaba la perdida correr hasta resolucion.

Logica:
  1) Cada ciclo lee Positions abiertas con strategy=whale_consensus.
  2) Para cada una, fetcha precio del CLOB (best_bid/ask).
  3) Calcula PnL%. Dispara cierre si:
       - PnL% >= take_profit_pct (proposal o default 0.12)
       - PnL% <= stop_loss_pct (proposal o default -0.08)
  4) Cierre intenta 2 estrategias en orden:
       a) GTC al best_bid/ask (limit que entra inmediato).
       b) Si rechaza por algo recuperable, precio agresivo (-2c bid o +2c ask).
       c) Si todo falla, marca como dust_exit y notifica TG.
          NUNCA deja correr la perdida hasta resolucion del mercado.

  Crea Trade record + actualiza Position.status=closed con pnl_realized.
  Update CopyTradeProposal.pnl si esta linkeada.

Llamado desde main.py cada iteracion (entre drain_pending_fills y scan_logical_arb).
"""

import logging
import time
from typing import Dict, Any, Optional

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from base44_client import (
    list_records,
    update_record,
    create_record,
    send_telegram,
    now_iso,
)
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

DEFAULT_TP_PCT = 0.12
DEFAULT_SL_PCT = -0.08
POLYMARKET_MIN_SHARES = 5.0
AGGRESSIVE_DISCOUNT_USD = 0.02


def _round_price(p: float) -> float:
    p = max(0.01, min(0.99, p))
    return round(p * 100) / 100.0


def _fetch_book(client, token_id: str) -> Optional[Dict[str, float]]:
    """Devuelve {bid, ask, mid} o None si falla."""
    try:
        book = client.get_order_book(token_id)
        best_bid = float(book.bids[0].price) if book and book.bids else 0.0
        best_ask = float(book.asks[0].price) if book and book.asks else 0.0
        if best_bid <= 0 or best_ask <= 0:
            return None
        return {
            "bid": best_bid,
            "ask": best_ask,
            "mid": (best_bid + best_ask) / 2.0,
        }
    except Exception as exc:
        logger.debug("get_order_book fallo en %s: %s", token_id[:12], exc)
        return None


def _compute_pnl_pct(entry: float, current: float, side: str) -> float:
    if entry <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry
    return (entry - current) / entry


def _try_close(client, token_id: str, side_str: str, shares: float,
               price: float) -> Dict[str, Any]:
    """
    Intenta cerrar shares al precio dado. Devuelve:
      {ok, filled_shares, filled_price, error}
    """
    if shares < POLYMARKET_MIN_SHARES:
        return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                "error": "size_below_min (%.2f<%.1f)" % (shares, POLYMARKET_MIN_SHARES)}
    try:
        # Para cerrar BUY position vendemos. Para cerrar SELL position compramos.
        close_side = SELL if side_str == "BUY" else BUY
        args = OrderArgs(
            token_id=token_id,
            price=_round_price(price),
            size=round(shares, 2),
            side=close_side,
        )
        signed = client.create_order(args)
        resp = client.post_order(signed, OrderType.GTC) or {}
        order_id = resp.get("orderID") or resp.get("orderId")
        success = bool(resp.get("success", True)) and resp.get("status") != "rejected"
        if not order_id or not success:
            return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                    "error": resp.get("error") or resp.get("status") or "rejected"}
        filled = 0.0
        try:
            status_resp = client.get_order(order_id) or {}
            filled = float(
                status_resp.get("size_matched")
                or status_resp.get("filled_size")
                or 0.0
            )
        except Exception:
            filled = shares
        if filled <= 0:
            return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                    "error": "no_fill"}
        return {"ok": True, "filled_shares": filled, "filled_price": price,
                "error": None, "order_id": order_id}
    except Exception as exc:
        return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                "error": str(exc)[:120]}


def _close_position(client, pos: Dict[str, Any], book: Dict[str, float],
                    reason: str, pnl_pct: float) -> bool:
    """Aplica fallback en cascada: GTC limit -> agresivo -> dust_exit."""
    pos_id = pos.get("id")
    token_id = pos.get("token_id")
    side_str = (pos.get("side") or "BUY").upper()
    entry = float(pos.get("entry_price") or 0.0)
    size_usdc = float(pos.get("size_usdc") or 0.0)
    market_label = (pos.get("question") or pos.get("market") or "?")[:60]

    # Para BUY salimos al bid. Para SELL salimos al ask.
    current_price = book["bid"] if side_str == "BUY" else book["ask"]
    shares = size_usdc / max(0.01, entry)

    attempts = []
    res = _try_close(client, token_id, side_str, shares, current_price)
    attempts.append({"strategy": "limit_at_book", "price": current_price})

    # 2do intento: precio agresivo si el fallo es recuperable
    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        agg_price = (current_price - AGGRESSIVE_DISCOUNT_USD if side_str == "BUY"
                     else current_price + AGGRESSIVE_DISCOUNT_USD)
        res = _try_close(client, token_id, side_str, shares, agg_price)
        attempts.append({"strategy": "aggressive", "price": agg_price})

    now_ts = time.time()

    if res["ok"]:
        exit_price = res["filled_price"]
        filled_shares = res["filled_shares"]
        notional_in = filled_shares * entry
        notional_out = filled_shares * exit_price
        pnl = (notional_out - notional_in) if side_str == "BUY" else (notional_in - notional_out)
        pnl = round(pnl, 4)
        update_record("Position", pos_id, {
            "status": "closed",
            "exit_price": exit_price,
            "current_price": exit_price,
            "close_reason": reason,
            "close_time": now_ts,
            "pnl_realized": pnl,
            "pnl_unrealized": 0.0,
        })
        create_record("Trade", {
            "market": pos.get("market") or market_label,
            "side": side_str,
            "entry_price": entry,
            "exit_price": exit_price,
            "size_usdc": round(filled_shares * entry, 2),
            "pnl": pnl,
            "pnl_pct": pnl_pct * 100,
            "strategy": pos.get("strategy") or "whale_consensus",
            "status": "closed",
            "entry_time": pos.get("opened_at") or now_iso(),
            "exit_time": now_iso(),
            "notes": "tp_sl_loop:%s:pnl%%=%.1f" % (reason, pnl_pct * 100),
        })
        try:
            linked = list_records(
                "CopyTradeProposal",
                limit=1,
                query={"executed_position_id": pos_id},
                sort="-created_date",
            )
            if linked:
                update_record("CopyTradeProposal", linked[0].get("id"), {"pnl": pnl})
        except Exception:
            pass
        emoji = "✅" if pnl > 0 else "🔴"
        send_telegram(
            "%s <b>%s</b> · $%+.2f\n"
            "%s\n"
            "%s %.3f → %.3f (%+.1f%%)\n"
            "Filled %.1f sh" % (
                emoji, reason.upper(), pnl, market_label,
                side_str, entry, exit_price, pnl_pct * 100, filled_shares,
            )
        )
        log_decision(
            reason="tp_sl_close_" + reason,
            market=market_label,
            strategy="whale_consensus",
            extra={"pos_id": pos_id, "pnl": pnl, "pnl_pct": pnl_pct,
                   "exit_price": exit_price, "attempts": len(attempts)},
        )
        return True

    # Todos los intentos fallaron -> dust_exit
    last_err = (res.get("error") or "unknown")[:80]
    update_record("Position", pos_id, {
        "status": "closed",
        "close_reason": "dust_exit",
        "close_time": now_ts,
        "current_price": current_price,
        "exit_price": current_price,
        "pnl_realized": 0.0,
        "pnl_unrealized": 0.0,
        "notes": (pos.get("notes") or "") +
                 " | dust_exit: " + last_err + " (intended " + reason + ")",
    })
    send_telegram(
        "⚠️ <b>DUST_EXIT</b> · no se pudo vender\n"
        "%s\n"
        "%s entry %.3f → mercado %.3f (%+.1f%%)\n"
        "Motivo intentado: <code>%s</code>\n"
        "Ultimo error CLOB: <code>%s</code>\n"
        "Posicion marcada cerrada con PnL=$0. Saldo on-chain queda hasta resolucion." % (
            market_label, side_str, entry, current_price, pnl_pct * 100,
            reason, last_err,
        )
    )
    log_warning(
        "tp_sl_dust_exit",
        module="position_tp_sl",
        extra={"pos_id": pos_id, "intended_reason": reason,
               "last_error": last_err, "pnl_pct": pnl_pct,
               "attempts": [a["strategy"] for a in attempts]},
    )
    return False


def manage_open_positions(client) -> Dict[str, int]:
    """
    Loop principal. Lee Positions abiertas whale_consensus, evalua TP/SL,
    cierra las que toquen aplicando fallback.
    Devuelve {checked, closed_tp, closed_sl, dust_exits}.
    """
    if client is None:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    positions = list_records(
        "Position",
        limit=50,
        query={"status": "open", "strategy": "whale_consensus"},
        sort="-opened_at",
    )
    if not positions:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    # Saltamos las que aun estan en pending_fill (no entraron en CLOB).
    positions = [p for p in positions if not p.get("pending_fill")]

    closed_tp = 0
    closed_sl = 0
    dust_exits = 0

    for pos in positions:
        token_id = pos.get("token_id")
        if not token_id:
            continue
        book = _fetch_book(client, token_id)
        if not book:
            continue

        entry = float(pos.get("entry_price") or 0.0)
        side_str = (pos.get("side") or "BUY").upper()
        exit_price_now = book["bid"] if side_str == "BUY" else book["ask"]
        pnl_pct = _compute_pnl_pct(entry, exit_price_now, side_str)

        # Mantener current_price actualizado para UI
        try:
            update_record("Position", pos.get("id"), {
                "current_price": exit_price_now,
                "pnl_unrealized": round(
                    float(pos.get("size_usdc") or 0.0) * pnl_pct, 4
                ),
            })
        except Exception:
            pass

        # TP/SL desde la proposal linkeada (sino defaults)
        tp_pct = DEFAULT_TP_PCT
        sl_pct = DEFAULT_SL_PCT
        try:
            linked = list_records(
                "CopyTradeProposal",
                limit=1,
                query={"executed_position_id": pos.get("id")},
                sort="-created_date",
            )
            if linked:
                p = linked[0]
                tp_pct = float(p.get("take_profit_pct") or DEFAULT_TP_PCT)
                sl_pct = float(p.get("stop_loss_pct") or DEFAULT_SL_PCT)
        except Exception:
            pass

        if pnl_pct >= tp_pct:
            ok = _close_position(client, pos, book, "take_profit", pnl_pct)
            if ok:
                closed_tp += 1
            else:
                dust_exits += 1
        elif pnl_pct <= sl_pct:
            ok = _close_position(client, pos, book, "stop_loss", pnl_pct)
            if ok:
                closed_sl += 1
            else:
                dust_exits += 1

    if closed_tp or closed_sl or dust_exits:
        logger.info("TP/SL loop: TP=%d SL=%d dust=%d (de %d posiciones)",
                    closed_tp, closed_sl, dust_exits, len(positions))

    return {
        "checked": len(positions),
        "closed_tp": closed_tp,
        "closed_sl": closed_sl,
        "dust_exits": dust_exits,
    }
