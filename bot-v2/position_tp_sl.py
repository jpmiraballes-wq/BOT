"""
position_tp_sl.py â TP/SL loop con fallback para Positions whale_consensus.

Por quÃ© existe:
  Los cierres `dust_unsellable` que vimos en Merida y Moutet ocurrÃ­an porque
  Polymarket exige mÃ­nimo 5 shares por orden. Cuando el bot intentaba cerrar
  una posiciÃ³n de $9-$30 a precios de salida bajos, las shares restantes
  caÃ­an bajo 5 â CLOB rechaza â el bot dejaba la pÃ©rdida correr hasta que
  el mercado resolvÃ­a solo.

LÃ³gica:
  1) Cada ciclo lee Positions abiertas (TODAS las strategies).
  2) Para cada una, fetcha precio del CLOB (best_bid/ask).
  3) Calcula PnL%. Dispara cierre si:
       - PnL% >= take_profit_pct (proposal o default 0.12)
       - PnL% <= stop_loss_pct (proposal o default -0.08)
  4) Cierre intenta cascada:
       a) GTC limit al best_bid/ask.
       b) Si "balance" â reduce -2% shares y reintenta.
       c) FAK cross-spread (cruza el ask/bid contrario, fill inmediato).
       d) Precio agresivo (-2c BUY / +2c SELL) con FAK.
       e) Si todo falla â dust_exit con PnL REAL contable (no $0).
          Crea Trade record para track record + recent_loss_block guard.
          Las shares quedan on-chain hasta resoluciÃ³n.
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
    """TP_SL_FORCED_EXIT_V2
    Devuelve {"bid": x, "ask": y, "mid": z} o None si NO hay precio utilizable.

    Fix Bencic 2026-04-27: antes rechazabamos libros con spread>0.50 o bid<0.02,
    eso bloqueaba SL en mercados iliquidos donde la posicion ya estaba sangrando.
    Ahora SOLO rechazamos si bid Y ask son 0 (book vacio total).
    """
    try:
        book = client.get_order_book(token_id)
        best_bid = float(book.bids[0].price) if book and book.bids else 0.0
        best_ask = float(book.asks[0].price) if book and book.asks else 0.0
        # Solo rechazamos si AMBOS lados estan vacios (book muerto total)
        if best_bid <= 0 and best_ask <= 0:
            logger.warning("book vacio en %s; no se puede operar", token_id[:12])
            return None
        # Si solo un lado tiene precio, derivamos el otro como fallback
        if best_bid <= 0 and best_ask > 0:
            best_bid = max(0.01, best_ask - 0.10)
            logger.warning("book sin bid en %s (ask=%.3f); estimando bid=%.3f para SL",
                           token_id[:12], best_ask, best_bid)
        if best_ask <= 0 and best_bid > 0:
            best_ask = min(0.99, best_bid + 0.10)
            logger.warning("book sin ask en %s (bid=%.3f); estimando ask=%.3f para SL",
                           token_id[:12], best_bid, best_ask)
        # Loggeo informativo si el spread es raro pero seguimos
        spread = best_ask - best_bid
        if spread > 0.30:
            logger.warning("spread amplio en %s (bid=%.3f ask=%.3f spread=%.3f) - SL puede igual disparar",
                           token_id[:12], best_bid, best_ask, spread)
        return {
            "bid": best_bid,
            "ask": best_ask,
            "mid": (best_bid + best_ask) / 2.0,
        }
    except Exception as exc:
        logger.warning("get_order_book fallo en %s: %s", token_id[:12], exc)
        return None

def _compute_pnl_pct(entry: float, current: float, side: str) -> float:
    if entry <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry
    return (entry - current) / entry


def _try_close(client, token_id: str, side_str: str, shares: float,
               price: float, order_type=None) -> Dict[str, Any]:
    """
    Intenta cerrar `shares` al precio dado. Devuelve dict con resultado.
    order_type: OrderType.GTC (default) o OrderType.FAK para cross-spread.
    """
    if order_type is None:
        order_type = OrderType.GTC
    if shares < POLYMARKET_MIN_SHARES:
        return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                "error": f"size_below_min ({shares:.2f}<{POLYMARKET_MIN_SHARES})"}
    try:
        close_side = SELL if side_str == "BUY" else BUY
        args = OrderArgs(
            token_id=token_id,
            price=_round_price(price),
            size=round(shares, 2),
            side=close_side,
        )
        signed = client.create_order(args)
        resp = client.post_order(signed, order_type) or {}
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
    """Cascada: GTC limit -> balance reduce -> FAK cross -> aggressive FAK -> dust_exit."""
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
    attempts.append({"strategy": "limit_at_book", "price": current_price,
                     "shares": shares})

    if not res["ok"] and "balance" in (res.get("error") or "").lower():
        reduced_shares = round(shares * 0.98, 2)
        res = _try_close(client, token_id, side_str, reduced_shares, current_price)
        attempts.append({"strategy": "balance_reduce_2pct", "price": current_price,
                         "shares": reduced_shares})
        if res["ok"]:
            shares = reduced_shares

    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        cross_price = current_price
        res = _try_close(client, token_id, side_str, shares, cross_price,
                         order_type=OrderType.FAK)
        attempts.append({"strategy": "fak_cross_spread", "price": cross_price,
                         "shares": shares})

    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        agg_price = (current_price - AGGRESSIVE_DISCOUNT_USD if side_str == "BUY"
                     else current_price + AGGRESSIVE_DISCOUNT_USD)
        res = _try_close(client, token_id, side_str, shares, agg_price,
                         order_type=OrderType.FAK)
        attempts.append({"strategy": "aggressive_fak", "price": agg_price,
                         "shares": shares})

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
            "notes": f"tp_sl_loop:{reason}:pnl%={pnl_pct*100:.1f}",
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
        emoji = "â" if pnl > 0 else "ð´"
        send_telegram(
            f"{emoji} <b>{reason.upper()}</b> Â· pnl=" + f"{pnl:+.4f}" + "\n"
            f"{market_label}\n"
            f"{side_str} {entry:.3f} â {exit_price:.3f} ({pnl_pct*100:+.1f}%)\n"
            f"Filled {filled_shares:.1f} sh"
        )
        log_decision(
            reason=f"tp_sl_close_{reason}",
            market=market_label,
            strategy="whale_consensus",
            extra={"pos_id": pos_id, "pnl": pnl, "pnl_pct": pnl_pct,
                   "exit_price": exit_price, "attempts": len(attempts)},
        )
        return True

    # Todos los intentos fallaron â dust_exit con PnL REAL.
    last_err = (res.get("error") or "unknown")[:80]
    estimated_shares = size_tokens_persisted if size_tokens_persisted > 0 else (
        size_usdc / max(0.01, entry)
    )
    notional_in = estimated_shares * entry
    notional_out = estimated_shares * current_price
    estimated_pnl = round(
        (notional_out - notional_in) if side_str == "BUY"
        else (notional_in - notional_out),
        4,
    )
    update_record("Position", pos_id, {
        "status": "closed",
        "close_reason": "dust_exit",
        "close_time": now_ts_iso,
        "current_price": current_price,
        "exit_price": current_price,
        "pnl_realized": estimated_pnl,
        "pnl_unrealized": 0.0,
        "notes": (pos.get("notes") or "") +
                 f" | dust_exit: {last_err} (intended {reason}, est_pnl={estimated_pnl:+.2f})",
    })
    try:
        create_record("Trade", {
            "market": pos.get("market") or market_label,
            "side": side_str,
            "entry_price": entry,
            "exit_price": current_price,
            "size_usdc": round(estimated_shares * entry, 2),
            "pnl": estimated_pnl,
            "pnl_pct": pnl_pct * 100,
            "strategy": pos.get("strategy") or "whale_consensus",
            "status": "closed",
            "entry_time": pos.get("opened_at") or now_iso(),
            "exit_time": now_iso(),
            "notes": f"tp_sl_dust_exit:{reason}:err={last_err}",
        })
    except Exception as exc:
        logger.error("dust_exit Trade create failed: %s", exc)
    try:
        linked = list_records(
            "CopyTradeProposal",
            limit=1,
            query={"executed_position_id": pos_id},
            sort="-created_date",
        )
        if linked:
            update_record("CopyTradeProposal", linked[0].get("id"),
                          {"pnl": estimated_pnl})
    except Exception:
        pass
    send_telegram(
        f"â ï¸ <b>DUST_EXIT</b> Â· no se pudo vender\n"
        f"{market_label}\n"
        f"{side_str} entry {entry:.3f} â mercado {current_price:.3f} "
        f"({pnl_pct*100:+.1f}%)\n"
        f"PnL contable: <b>{estimated_pnl:+.2f} USDC</b>\n"
        f"Motivo intentado: <code>{reason}</code>\n"
        f"Ãltimo error CLOB: <code>{last_err}</code>\n"
        f"Saldo on-chain queda hasta resoluciÃ³n."
    )
    log_warning(
        "tp_sl_dust_exit",
        module="position_tp_sl",
        extra={"pos_id": pos_id, "intended_reason": reason,
               "last_error": last_err, "pnl_pct": pnl_pct,
               "estimated_pnl": estimated_pnl,
               "attempts": [a["strategy"] for a in attempts]},
    )
    return False


def manage_open_positions(client) -> Dict[str, int]:
    """Loop principal. TODAS las strategies."""
    if client is None:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    # FIX 2026-04-26 JP: monitorea TODAS las strategies (antes solo whale_consensus).
    positions = list_records("Position", limit=50, query={"status": "open"}, sort="-opened_at")
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
                "pnl_unrealized": round(
                    float(pos.get("size_usdc") or 0.0) * pnl_pct, 4
                ),
            })
        except Exception:
            pass

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
