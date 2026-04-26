"""
position_tp_sl.py - TP/SL loop con fallback para Positions whale_consensus.
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
    """FIX 2026-04-26 v2: max(bids)/min(asks) en vez de [0]."""
    try:
        book = client.get_order_book(token_id)
        best_bid = max((float(b.price) for b in book.bids), default=0.0) if book and book.bids else 0.0
        best_ask = min((float(a.price) for a in book.asks), default=0.0) if book and book.asks else 0.0
        if best_bid <= 0 or best_ask <= 0:
            return None
        if best_bid < 0.02 and best_ask < 0.02:
            logger.warning("book sospechoso en %s (bid=%.4f ask=%.4f); ignorando", token_id[:12], best_bid, best_ask)
            return None
        if (best_ask - best_bid) > 0.50:
            logger.warning("spread anomalo en %s (bid=%.3f ask=%.3f); ignorando", token_id[:12], best_bid, best_ask)
            return None
        return {"bid": best_bid, "ask": best_ask, "mid": (best_bid + best_ask) / 2.0}
    except Exception as exc:
        logger.debug("get_order_book fallo en %s: %s", token_id[:12], exc)
        return None


def _compute_pnl_pct(entry: float, current: float, side: str) -> float:
    if entry <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry
    return (entry - current) / entry


def _try_close(client, token_id: str, side_str: str, shares: float, price: float) -> Dict[str, Any]:
    if shares < POLYMARKET_MIN_SHARES:
        return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0, "error": "size_below_min"}
    try:
        close_side = SELL if side_str == "BUY" else BUY
        args = OrderArgs(token_id=token_id, price=_round_price(price), size=round(shares, 2), side=close_side)
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
            filled = float(status_resp.get("size_matched") or status_resp.get("filled_size") or 0.0)
        except Exception:
            filled = shares
        if filled <= 0:
            return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0, "error": "no_fill"}
        return {"ok": True, "filled_shares": filled, "filled_price": price, "error": None, "order_id": order_id}
    except Exception as exc:
        return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0, "error": str(exc)[:120]}


def _close_position(client, pos: Dict[str, Any], book: Dict[str, float], reason: str, pnl_pct: float) -> bool:
    pos_id = pos.get("id")
    token_id = pos.get("token_id")
    side_str = (pos.get("side") or "BUY").upper()
    entry = float(pos.get("entry_price") or 0.0)
    size_usdc = float(pos.get("size_usdc") or 0.0)
    market_label = (pos.get("question") or pos.get("market") or "?")[:60]
    current_price = book["bid"] if side_str == "BUY" else book["ask"]

    size_tokens_persisted = float(pos.get("size_tokens") or 0.0)
    if size_tokens_persisted > 0:
        shares = size_tokens_persisted
    else:
        shares = size_usdc / max(0.01, entry)

    attempts = []
    res = _try_close(client, token_id, side_str, shares, current_price)
    attempts.append({"strategy": "limit_at_book"})

    if not res["ok"] and "balance" in (res.get("error") or "").lower():
        reduced_shares = round(shares * 0.98, 2)
        res = _try_close(client, token_id, side_str, reduced_shares, current_price)
        attempts.append({"strategy": "balance_reduce_2pct"})
        if res["ok"]:
            shares = reduced_shares

    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        agg_price = (current_price - AGGRESSIVE_DISCOUNT_USD if side_str == "BUY" else current_price + AGGRESSIVE_DISCOUNT_USD)
        res = _try_close(client, token_id, side_str, shares, agg_price)
        attempts.append({"strategy": "aggressive"})

    # FIX 2026-04-26 v3: close_time como ISO string. Base44 schema lo declara
    # como format=date-time; mandar time.time() (float) da 422 ValidationError
    # y ni el dust_exit logra cerrar la posicion en DB.
    now_ts_iso = now_iso()

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
            "close_time": now_ts_iso,
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
            "notes": "tp_sl_loop:" + str(reason) + ":pnl%=" + str(round(pnl_pct*100, 1)),
        })
        try:
            linked = list_records("CopyTradeProposal", limit=1, query={"executed_position_id": pos_id}, sort="-created_date")
            if linked:
                update_record("CopyTradeProposal", linked[0].get("id"), {"pnl": pnl})
        except Exception:
            pass
        emoji = "WIN" if pnl > 0 else "LOSS"
        send_telegram(
            emoji + " " + str(reason).upper() + " $" + ("%+.2f" % pnl) + "\n" +
            market_label + "\n" +
            side_str + " " + ("%.3f" % entry) + " -> " + ("%.3f" % exit_price) +
            " (" + ("%+.1f" % (pnl_pct*100)) + "%)\n" +
            "Filled " + ("%.1f" % filled_shares) + " sh"
        )
        log_decision(
            reason="tp_sl_close_" + str(reason),
            market=market_label,
            strategy="whale_consensus",
            extra={"pos_id": pos_id, "pnl": pnl, "pnl_pct": pnl_pct,
                   "exit_price": exit_price, "attempts": len(attempts)},
        )
        return True

    last_err = (res.get("error") or "unknown")[:80]
    update_record("Position", pos_id, {
        "status": "closed",
        "close_reason": "dust_exit",
        "close_time": now_ts_iso,
        "current_price": current_price,
        "exit_price": current_price,
        "pnl_realized": 0.0,
        "pnl_unrealized": 0.0,
        "notes": (pos.get("notes") or "") + " | dust_exit: " + last_err + " (intended " + str(reason) + ")",
    })
    send_telegram(
        "DUST_EXIT no se pudo vender\n" +
        market_label + "\n" +
        side_str + " entry " + ("%.3f" % entry) + " -> mercado " + ("%.3f" % current_price) +
        " (" + ("%+.1f" % (pnl_pct*100)) + "%)\n" +
        "Motivo intentado: " + str(reason) + "\n" +
        "Ultimo error CLOB: " + last_err + "\n" +
        "Posicion marcada cerrada con PnL=$0. Saldo on-chain queda hasta resolucion."
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
    if client is None:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    positions = list_records("Position", limit=50, query={"status": "open", "strategy": "whale_consensus"}, sort="-opened_at")
    if not positions:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

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

        try:
            update_record("Position", pos.get("id"), {
                "current_price": exit_price_now,
                "pnl_unrealized": round(float(pos.get("size_usdc") or 0.0) * pnl_pct, 4),
            })
        except Exception:
            pass

        tp_pct = DEFAULT_TP_PCT
        sl_pct = DEFAULT_SL_PCT
        try:
            linked = list_records("CopyTradeProposal", limit=1, query={"executed_position_id": pos.get("id")}, sort="-created_date")
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
        logger.info("TP/SL loop: TP=%d SL=%d dust=%d (de %d posiciones)", closed_tp, closed_sl, dust_exits, len(positions))

    return {"checked": len(positions), "closed_tp": closed_tp, "closed_sl": closed_sl, "dust_exits": dust_exits}
