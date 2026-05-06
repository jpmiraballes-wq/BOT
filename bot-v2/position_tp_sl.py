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
import requests
from typing import Dict, Any, Optional

from _clob_compat import OrderArgs, OrderType
from _clob_compat import BUY, SELL

from base44_client import (
    list_records,
    update_record,
    create_record,
    send_telegram,
    now_iso,
)
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

# ON_CHAIN_BALANCE_GUARD_V1 (JP+Opus 2026-05-04):
# Antes de intentar vender, leer el balance REAL on-chain para no errar con
# "not enough balance". El BUY original cobra ~2% en shares, así que size_tokens
# de la DB queda inflado. Caso Lazio 2026-05-04: DB decia 32 tokens, on-chain 24
# -> 4 capas de cascada fallaron con balance error -> posicion quedo zombie.
_TP_SL_WALLET = "0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb"

def _fetch_onchain_balance(token_id):
    """Devuelve shares reales on-chain para este token. 0.0 si falla o no existe."""
    if not token_id:
        return 0.0
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": _TP_SL_WALLET, "limit": 500},
            timeout=8,
        )
        if not r.ok:
            return 0.0
        for p in (r.json() or []):
            asset = str(p.get("asset") or p.get("tokenId") or p.get("token_id") or "")
            if asset == str(token_id):
                return float(p.get("size") or 0.0)
        return 0.0
    except Exception as e:
        logger.warning("onchain_balance fetch failed for %s: %s", str(token_id)[:12], e)
        return 0.0

DEFAULT_TP_PCT = 0.95  # PROFIT_RUNNER_35_12_V1: TP fijo practicamente apagado; dejar correr ganadoras con trailing
DEFAULT_SL_PCT = -0.35  # PROFIT_RUNNER_35_12_V1: stop inicial amplio; no vender por ruido inicial -8/-12/-20%
# TRAILING_STOP_V1: trailing stop dinámico. Cuando max_pnl_pct (HWM) supera
# TRAILING_ACTIVATION_PCT, el SL efectivo pasa a max_pnl_pct - TRAILING_DISTANCE_PCT.
# Mientras max_pnl_pct < TRAILING_ACTIVATION_PCT se usa el SL fijo (default_sl).
TRAILING_ACTIVATION_PCT = 0.10  # PROFIT_RUNNER_35_12_V1: trailing solo cuando ya gana +10%
TRAILING_DISTANCE_PCT = 0.12  # PROFIT_RUNNER_35_12_V1: vender solo si cae 12% desde el maximo
POLYMARKET_MIN_SHARES = 5.0
AGGRESSIVE_DISCOUNT_USD = 0.02
PENDING_FILL_GHOST_MAX_AGE_SEC = 10 * 60
PENDING_FILL_MIN_ONCHAIN_BALANCE = 0.01

# TP_SL_MID_DOUBLE_CONFIRM_V1 — Bolt audit 2026-04-27
# Doble confirmación para SL catastrófico (<-40%).
# TP_SL_THRESHOLD_AGE_V1 FIX A — JP 2026-04-27: bajado de -0.40 a -0.20.
# Caso Tsitsipas: SL disparó a -29.6% en 41s post-fill, threshold -40% NO lo
# atrapó. -20% sí captura el caso y obliga a esperar 30s + 2da lectura.
SL_CATASTROPHIC_THRESHOLD = -0.20
SL_CONFIRM_SECONDS = 30.0
_sl_pending: Dict[str, float] = {}

# TP_SL_THRESHOLD_AGE_V1 FIX B — JP 2026-04-27: trade_min_age 90s.
# Los libros CLOB post-fill son ruidosos los primeros ~90s.
# No dispara SL hasta que el book se estabilice.
TRADE_MIN_AGE_SECONDS = 90.0

# PANIC_EXIT_V1 — Bolt+JP+Opus 2026-04-27 noche
# Panic exit para favoritos desplomados (caso Bencic 71→21¢).
# Si pnl_pct <= -30% y trade tiene >5min de vida → vender al market sin
# doble confirmación. Mejor -35% realizado que -80% mirando.
# Bypassea _is_too_young y _should_block_sl (esos guards solo aplican a stop_loss).
PANIC_DRAWDOWN_THRESHOLD = -0.30
PANIC_MIN_AGE_SECONDS = 300.0



def _best_level_price(book, side: str) -> float:
    """Lee el mejor precio del order book devuelto por py_clob_client_v2.

    V2 devuelve dict ({"bids": [{"price": "0.49", "size": "..."}, ...], "asks": [...]}).
    El order book V2 viene ordenado peor->mejor (bids ascendente, asks descendente),
    asi que tomamos max() para bids y min() para asks. Robusto tambien a obj-attr (V1)
    y al orden inverso.
    """
    if not book:
        return 0.0
    try:
        levels = book[side] if isinstance(book, dict) else getattr(book, side, None)
    except Exception:
        levels = None
    if not levels:
        return 0.0
    prices = []
    for lvl in levels:
        try:
            raw = lvl["price"] if isinstance(lvl, dict) else getattr(lvl, "price", None)
        except Exception:
            raw = None
        if raw is None:
            continue
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not prices:
        return 0.0
    # bids -> el mas alto es el mejor; asks -> el mas bajo es el mejor.
    return max(prices) if side == "bids" else min(prices)


def _is_too_young(pos: Dict[str, Any]) -> bool:
    """True = el trade es muy joven, NO disparar SL."""
    opened_ts = pos.get("opened_at_ts")
    if not opened_ts:
        return False
    try:
        age = time.time() - float(opened_ts)
        if age < TRADE_MIN_AGE_SECONDS:
            logger.info(
                "TRADE_MIN_AGE: pos %s age=%.1fs < %.0fs, SL bloqueado",
                str(pos.get("id", ""))[:8], age, TRADE_MIN_AGE_SECONDS,
            )
            return True
    except Exception:
        return False
    return False


def _should_block_sl(pos_id: str, pnl_pct: float) -> bool:
    """True = bloquear SL (esperar segunda confirmación). False = dejar pasar."""
    if pnl_pct >= SL_CATASTROPHIC_THRESHOLD:
        _sl_pending.pop(pos_id, None)
        return False
    now_ts = time.time()
    first_ts = _sl_pending.get(pos_id)
    if first_ts is None:
        _sl_pending[pos_id] = now_ts
        logger.warning(
            "SL_DOUBLE_CONFIRM: primera lectura pnl=%.1f%% en %s, esperando %.0fs",
            pnl_pct * 100, pos_id[:8], SL_CONFIRM_SECONDS,
        )
        return True
    elapsed = now_ts - first_ts
    if elapsed < SL_CONFIRM_SECONDS:
        logger.warning(
            "SL_DOUBLE_CONFIRM: aún esperando, %.0fs/%.0fs en %s",
            elapsed, SL_CONFIRM_SECONDS, pos_id[:8],
        )
        return True
    _sl_pending.pop(pos_id, None)
    logger.warning(
        "SL_DOUBLE_CONFIRM: confirmado pnl=%.1f%% en %s tras %.0fs, disparando SL",
        pnl_pct * 100, pos_id[:8], elapsed,
    )
    return False


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
        best_bid = _best_level_price(book, "bids")
        best_ask = _best_level_price(book, "asks")
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


def _mark_sell_state(pos_id: str, state: str, reason: str, order_id: str = "", extra: str = "") -> None:
    """TP_SL_CONFIRMED_CLOSE_V1: estado auditable del cierre. Nunca inventa closed."""
    if not pos_id:
        return
    patch = {
        "status": state,
        "close_reason": state if not reason else f"{state}:{reason}",
        "notes": f"TP_SL_CONFIRMED_CLOSE_V1 {state} order_id={order_id or '-'} {extra}".strip(),
    }
    try:
        update_record("Position", pos_id, patch)
        logger.warning("TP_SL_CONFIRMED_CLOSE:%s pos=%s order=%s reason=%s %s",
                       state, pos_id[:8], order_id or '-', reason, extra)
    except Exception as exc:
        logger.warning("TP_SL_CONFIRMED_CLOSE: state update failed pos=%s state=%s err=%s",
                       pos_id[:8], state, str(exc)[:100])


# PENDING_FILL_GHOST_GUARD_V1 (JP+Opus 2026-05-05):
# Si cloud creó Position pending_fill=true pero el bot Mac/CLOB nunca dejó balance
# on-chain, cerramos la Position como fantasma. Evita que FAST_PATH_DB_CREATED
# quede como Position open eterna sin haber llegado a Polymarket.
def _position_age_seconds(pos: Dict[str, Any]) -> float:
    raw = pos.get("opened_at_ts")
    if raw:
        try:
            return time.time() - float(raw)
        except Exception:
            pass
    raw = pos.get("opened_at") or pos.get("created_date")
    if not raw:
        return 0.0
    try:
        from datetime import datetime
        return time.time() - datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _reconcile_pending_fill_ghosts(positions) -> Dict[str, int]:
    stats = {"pending_ghost_closed": 0, "pending_fill_confirmed": 0}
    for pos in positions or []:
        if not pos.get("pending_fill"):
            continue
        if _position_age_seconds(pos) < PENDING_FILL_GHOST_MAX_AGE_SEC:
            continue

        pos_id = pos.get("id")
        token_id = pos.get("token_id")
        balance = _fetch_onchain_balance(token_id)

        if balance <= PENDING_FILL_MIN_ONCHAIN_BALANCE:
            update_record("Position", pos_id, {
                "status": "closed",
                "pending_fill": False,
                "close_reason": "tx_failed_onchain",
                "close_time": now_iso(),
                "pnl_realized": 0.0,
                "pnl_unrealized": 0.0,
                "notes": (pos.get("notes") or "") + " | PENDING_FILL_GHOST_GUARD_V1: no on-chain balance after 10min",
            })
            log_warning(
                "pending_fill_ghost_closed",
                module="position_tp_sl",
                extra={"pos_id": pos_id, "token_id": str(token_id)[:12], "balance": balance},
            )
            stats["pending_ghost_closed"] += 1
        else:
            update_record("Position", pos_id, {
                "pending_fill": False,
                "size_tokens": round(balance, 4),
                "notes": (pos.get("notes") or "") + f" | PENDING_FILL_GHOST_GUARD_V1: confirmed on-chain balance={balance:.4f}",
            })
            stats["pending_fill_confirmed"] += 1
    return stats


def _try_close(client, token_id: str, side_str: str, shares: float,
               price: float, order_type=None, pos_id: Optional[str] = None,
               reason: str = "") -> Dict[str, Any]:
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
        _mark_sell_state(pos_id, "sell_submitted", reason, order_id,
                         f"order_type={str(order_type)} price={_round_price(price):.2f} shares={shares:.2f}")
        # TP_SL_FILL_VERIFY_V1: si get_order falla, NO asumimos fill completo. Antes
        # del fix: except Exception: filled = shares → escribió Trade fantasma
        # cuando post_order acepta pero el SELL no llega a fillarse on-chain
        # (timeouts CLOB, rate limit, 500 momentáneo). Caso Saint-Malo +
        # Krueger 2026-04-29: shares siguieron en wallet, DB marcó Position
        # closed con pnl negativo falso. Ahora: si verify falla → ok=False,
        # cae a la siguiente capa de cascada o a dust_unsellable (que ya no
        # crea Trade fantasma desde fix Bolt 2026-04-27).
        filled = 0.0
        verify_failed = False
        try:
            status_resp = client.get_order(order_id) or {}
            filled = float(
                status_resp.get("size_matched")
                or status_resp.get("filled_size")
                or 0.0
            )
        except Exception as exc:
            verify_failed = True
            logger.warning(
                "TP_SL_FILL_VERIFY: get_order(%s) failed: %s. NOT assuming fill.",
                order_id, str(exc)[:80],
            )
        if verify_failed:
            return {"ok": False, "filled_shares": 0.0, "filled_price": 0.0,
                    "error": f"verify_failed:get_order_exception", "order_id": order_id}
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
    # TP_SL_THRESHOLD_AGE_V1 FIX B: bloquear SL si el trade tiene <90s de vida (book ruidoso).
    if reason == "stop_loss" and _is_too_young(pos):
        return False
    # TP_SL_MID_DOUBLE_CONFIRM_V1 + TP_SL_THRESHOLD_AGE_V1 FIX A: bloquear SL <-20% sin doble confirmación.
    if reason == "stop_loss" and pos_id and _should_block_sl(pos_id, pnl_pct):
        return False
    token_id = pos.get("token_id")
    side_str = (pos.get("side") or "BUY").upper()
    entry = float(pos.get("entry_price") or 0.0)
    size_usdc = float(pos.get("size_usdc") or 0.0)
    market_label = (pos.get("question") or pos.get("market") or "?")[:60]

    # TP_SL_MID_DOUBLE_CONFIRM_V1 — Bolt audit 2026-04-27
    # Antes: book["bid"] BUY / book["ask"] SELL → bid hundido a 1¢ con ask 50¢
    # daba pnl=-98% y liquidaba. Fix: usar mid (precio real del mercado).
    current_price = book["mid"]

    size_tokens_persisted = float(pos.get("size_tokens") or 0.0)
    db_shares = size_tokens_persisted if size_tokens_persisted > 0 else (size_usdc / max(0.01, entry))

    # ON_CHAIN_BALANCE_GUARD_V1: usar el menor entre DB y on-chain real.
    # Si on-chain devuelve 0 -> token no existe (orden nunca filleo o ya se vendio),
    # no intentamos vender, dejamos que el reconcile lo limpie.
    on_chain_shares = _fetch_onchain_balance(token_id)
    if on_chain_shares <= 0:
        logger.warning("tp_sl skip: pos %s has no on-chain balance (db_shares=%.2f)",
                       str(pos.get("id", ""))[:8], db_shares)
        return False

    shares = min(db_shares, on_chain_shares)
    if shares < db_shares:
        logger.info("tp_sl shares adjusted: db=%.2f -> on_chain=%.2f for %s",
                    db_shares, on_chain_shares, market_label[:30])

    attempts = []
    res = _try_close(client, token_id, side_str, shares, current_price,
                     pos_id=pos_id, reason=reason)
    attempts.append({"strategy": "limit_at_book", "price": current_price,
                     "shares": shares})

    if not res["ok"] and "balance" in (res.get("error") or "").lower():
        reduced_shares = round(shares * 0.98, 2)
        res = _try_close(client, token_id, side_str, reduced_shares, current_price,
                         pos_id=pos_id, reason=reason)
        attempts.append({"strategy": "balance_reduce_2pct", "price": current_price,
                         "shares": reduced_shares})
        if res["ok"]:
            shares = reduced_shares

    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        cross_price = current_price
        res = _try_close(client, token_id, side_str, shares, cross_price,
                         order_type=OrderType.FAK, pos_id=pos_id, reason=reason)
        attempts.append({"strategy": "fak_cross_spread", "price": cross_price,
                         "shares": shares})

    if not res["ok"] and "size_below_min" not in (res.get("error") or ""):
        # TP_SL_MID_DOUBLE_CONFIRM_V1: floor 5¢ BUY / 95¢ SELL (Bolt audit 2026-04-27).
        # Antes podía caer a 1¢ si current_price era basura. No regalamos shares.
        agg_price = (max(0.05, current_price - AGGRESSIVE_DISCOUNT_USD)
                     if side_str == "BUY"
                     else min(0.95, current_price + AGGRESSIVE_DISCOUNT_USD))
        res = _try_close(client, token_id, side_str, shares, agg_price,
                         order_type=OrderType.FAK, pos_id=pos_id, reason=reason)
        attempts.append({"strategy": "aggressive_fak", "price": agg_price,
                         "shares": shares})

    now_ts_iso = now_iso()

    if res["ok"]:
        # TP_SL_FILLED_PRICE_SANITY_V1 — JP+Opus 2026-04-27.
        # CAUSA RAIZ Trade fantasma Tsitsipas/Andreeva/Cerundolo:
        # El CLOB a veces devuelve filled_price corrupto (ej 0.50 cuando vendiste
        # a 0.79). Si confiamos ciego, escribimos PnL fantasma -98% al Trade.
        # FIX: validar filled_price contra current_price (mid del book).
        # Si drift > 30 centavos, es sospechoso. Usamos current_price.
        raw_filled_price = float(res["filled_price"] or 0.0)
        sanity_drift = abs(raw_filled_price - current_price)
        if raw_filled_price <= 0.0 or sanity_drift > 0.30:
            logger.warning(
                "FILLED_PRICE_SANITY: pos %s reportó filled_price=%.4f vs mid=%.4f "
                "(drift %.4f). Usando mid para evitar Trade fantasma.",
                str(pos.get("id", ""))[:8], raw_filled_price, current_price, sanity_drift,
            )
            exit_price = current_price
        else:
            exit_price = raw_filled_price
        filled_shares = res["filled_shares"]
        # TP_SL_CONFIRMED_CLOSE_V1: get_order confirmó fill, pero NO cerramos todavía.
        # Verificamos balance real en Polymarket data-api. Si quedan shares,
        # fue parcial o el data-api todavía no reflejó el SELL: mantener viva.
        remaining_shares = _fetch_onchain_balance(token_id)
        if remaining_shares > 0.01:
            _mark_sell_state(
                pos_id,
                "sell_partially_filled",
                reason,
                res.get("order_id") or "",
                f"filled={filled_shares:.2f} remaining={remaining_shares:.2f}",
            )
            update_record("Position", pos_id, {
                "status": "sell_partially_filled",
                "size_tokens": remaining_shares,
                "current_price": current_price,
                "pnl_unrealized": 0.0,
            })
            return False
        _mark_sell_state(pos_id, "sell_confirmed", reason, res.get("order_id") or "",
                         f"filled={filled_shares:.2f} remaining=0")
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
        # TELEGRAM_ONLY_WINS_V1: solo Telegram cuando es ganancia. Losses, breakeven y
        # cierres negativos van solo al Trade record y log_decision (silencioso).
        if pnl > 0:
            send_telegram(
                f"â <b>{reason.upper()}</b> Â· pnl=" + f"{pnl:+.4f}" + "\n"
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

    # TP_SL_CONFIRMED_CLOSE_V1: si NO hay Sell/Redeem confirmado, la Position NO se cierra.
    # Antes este branch marcaba status=closed con dust_unsellable aunque los
    # tokens siguieran on-chain. Eso generaba posiciones fantasma y PnL falso.
    last_err = (res.get("error") or "unknown")[:80]
    remaining_shares = _fetch_onchain_balance(token_id)
    update_record("Position", pos_id, {
        "status": "open",
        "close_reason": "sell_unfilled",
        "size_tokens": remaining_shares if remaining_shares > 0 else shares,
        "current_price": current_price,
        "pnl_realized": 0.0,
        "pnl_unrealized": 0.0,
        "notes": (pos.get("notes") or "") +
                 f" | TP_SL_CONFIRMED_CLOSE_V1: sell_unfilled: {last_err} (intended {reason}; "
                 f"remaining_shares={remaining_shares:.2f}; NO Trade creado)",
    })
    try:
        logger.warning("dust_unsellable: %s reason=%s err=%s", pos_id[:8], reason, last_err)
    except Exception as exc:
        logger.error("dust_unsellable log failed: %s", exc)
    try:
        linked = list_records(
            "CopyTradeProposal",
            limit=1,
            query={"executed_position_id": pos_id},
            sort="-created_date",
        )
        if linked:
            # TP_SL_ESTIMATED_PNL_FIX_V1: estimated_pnl no se declara en este branch.
            # Coherente con "NO inventamos PnL" — usamos 0.0 literal.
            update_record("CopyTradeProposal", linked[0].get("id"),
                          {"pnl": 0.0})
    except Exception:
        pass
    # TELEGRAM_ONLY_WINS_V1: DUST_EXIT silenciado. pnl=0 por diseÃ±o (no hubo venta), no es
    # ganancia. log_warning de abajo queda para auditorÃ­a interna.
    log_warning(
        "tp_sl_dust_exit",
        module="position_tp_sl",
        extra={"pos_id": pos_id, "intended_reason": reason,
               "last_error": last_err, "pnl_pct": pnl_pct,
               "estimated_pnl": 0.0,
               "attempts": [a["strategy"] for a in attempts]},
    )
    return False


# GUARD_TIME_TO_RESOLVE_V1 (JP+Opus 2026-05-04 noche):
# Antes de disparar TP fijo (35%) chequeamos cuánto falta para que el mercado
# resuelva. Si faltan más de 90min → ignoramos TP fijo y dejamos correr el
# trailing. Si faltan ≤90min o no hay end_date conocido → TP fijo activa normal.
# Caso Manchester City 4-may: vendimos a +22% cuando el partido recién había
# arrancado y faltaban 100min. Si City ganaba se nos iban $24 más en mesa.
GUARD_TIME_TO_RESOLVE_MIN = 90

def _minutes_to_resolve(pos):
    """Devuelve minutos hasta la resolución, o None si no se puede determinar.

    Lee CopyTradeProposal.market_end_date asociada a la Position vía
    executed_position_id. El cron guarda end_date desde gamma-api de Polymarket.
    Si no hay proposal vinculada o end_date inválido → None (TP fallback activo).
    """
    try:
        linked = list_records(
            "CopyTradeProposal",
            limit=1,
            query={"executed_position_id": pos.get("id")},
            sort="-created_date",
        )
        if not linked:
            return None
        end_iso = linked[0].get("market_end_date")
        if not end_iso:
            return None
        from datetime import datetime, timezone
        # Acepta tanto "2026-05-04T20:00:00Z" como "2026-05-04T20:00:00+00:00"
        end_dt = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        delta_min = (end_dt - now_dt).total_seconds() / 60.0
        return delta_min
    except Exception as e:
        logger.debug("guard_time_to_resolve: failed for pos=%s err=%s",
                     str(pos.get("id", ""))[:8], e)
        return None



# SWISSTONY_MIRROR_MODE_V1_TPSL: si Swisstony vende el mismo token que copiamos, salimos.
# Esto convierte el sistema de copy-buy en mirror real BUY+SELL.
def _find_recent_swisstony_sell(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_id = pos.get("token_id")
    if not token_id:
        return None
    try:
        signals = list_records(
            "WhaleSignal",
            limit=20,
            query={"token_id": token_id, "side": "SELL"},
            sort="-whale_trade_ts",
        )
    except Exception:
        return None

    opened_ts = float(pos.get("opened_at_ts") or 0.0)
    now_ts = time.time()
    for sig in signals or []:
        name = str(sig.get("whale_name") or "").lower()
        addr = str(sig.get("whale_address") or "").lower()
        is_swisstony = "swisstony" in name or "swiss_tony" in name or addr == "0x204f72f35326db932158cba6adff0b9a1da95e14"
        if not is_swisstony:
            continue
        sell_ts = float(sig.get("whale_trade_ts") or 0.0)
        if sell_ts <= 0:
            continue
        # Solo cuenta ventas posteriores a nuestra apertura, y no señales viejas.
        if opened_ts and sell_ts < opened_ts - 10:
            continue
        if now_ts - sell_ts > 2 * 3600:
            continue
        return sig
    return None

# MIRROR_ONLY_ON_LOSS_V1: calcula el PnL aproximado del whale para decidir si
# copiamos su SELL. Asumimos que nuestro entry_price ≈ swisstony entry_price
# (somos mirror copy), así que (sell.price - our_entry) / our_entry ≈ swisstony_pnl_pct.
# Solo devolvemos True si swisstony está saliendo con pérdida ≤ -20%.
def _mirror_loss_qualifies(pos: Dict[str, Any], sell_signal: Optional[Dict[str, Any]]) -> bool:
    if not sell_signal:
        return False
    try:
        sell_price = float(sell_signal.get("price") or 0.0)
        our_entry = float(pos.get("entry_price") or 0.0)
        if sell_price <= 0 or our_entry <= 0:
            return False
        side = (pos.get("side") or "BUY").upper()
        # Para BUY: ganamos si sell_price > entry. Pérdida si < entry.
        # Para SELL (rare en copy): invertido.
        if side == "BUY":
            whale_pnl_pct = (sell_price - our_entry) / our_entry
        else:
            whale_pnl_pct = (our_entry - sell_price) / our_entry
        qualifies = whale_pnl_pct <= -0.2
        logger.info(
            "MIRROR_GATE: pos=%s whale_sell=%.4f our_entry=%.4f whale_pnl=%.2f%% qualifies=%s",
            str(pos.get("id", ""))[:8],
            sell_price,
            our_entry,
            whale_pnl_pct * 100,
            qualifies,
        )
        return bool(qualifies)
    except Exception as e:
        logger.debug("mirror_loss_qualifies failed: %s", e)
        return False


def manage_open_positions(client) -> Dict[str, int]:
    """Loop principal. TODAS las strategies."""
    if client is None:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    # FIX 2026-04-26 JP: monitorea TODAS las strategies (antes solo whale_consensus).
    # TP_SL_MANAGE_PARTIAL_FILLS_V1: gestionar posiciones abiertas y ventas parciales.
    # sell_submitted no se toca hasta próximo ciclo/reconcile; sell_partially_filled
    # mantiene remaining size_tokens y vuelve a intentar salida si TP/SL sigue activo.
    open_positions = list_records("Position", limit=50, query={"status": "open"}, sort="-opened_at")
    partial_positions = list_records("Position", limit=50, query={"status": "sell_partially_filled"}, sort="-updated_date")
    positions = (open_positions or []) + (partial_positions or [])
    # TP_SL_CONFIGURABLE_FROM_BOTCONFIG_V1: leer SL/TP desde BotConfig en cada ciclo. Null-check explícito
    # — 0 no cae al fallback (decisión JP 2026-04-30 Madrid).
    cfg_rows = list_records("BotConfig", limit=1)
    cfg = cfg_rows[0] if cfg_rows else {}
    cfg_sl = cfg.get("stop_loss")
    cfg_tp = cfg.get("take_profit")
    default_sl = float(cfg_sl) if cfg_sl is not None else DEFAULT_SL_PCT
    default_tp = float(cfg_tp) if cfg_tp is not None else DEFAULT_TP_PCT
    if not positions:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}
    # PENDING_FILL_GHOST_GUARD_V1: antes de saltarlas, reconciliamos las que
    # llevan >10min pending_fill=true contra balance real on-chain.
    pending_stats = _reconcile_pending_fill_ghosts(positions)

    # Saltamos las que aún están en pending_fill reciente.
    positions = [p for p in positions if not p.get("pending_fill")]

    closed_tp = 0
    closed_sl = 0
    dust_exits = 0
    closed_mirror = 0

    for pos in positions:
        token_id = pos.get("token_id")
        if not token_id:
            continue

        # RECONCILE_SELL_UNFILLED_V1: si ya intentó vender, verificar balance real antes de reentrar
        if pos.get("close_reason") == "sell_unfilled":
            remaining = _fetch_onchain_balance(token_id)
            if remaining <= 0.01:
                # Polymarket ya vendió — construir book sintético con precio actual
                book = _fetch_book(client, token_id)
                if not book:
                    entry = float(pos.get("entry_price") or 0.5)
                    book = {"best_ask": entry, "best_bid": entry, "mid": entry}
                entry = float(pos.get("entry_price") or 0)
                pnl_pct = _compute_pnl_pct(entry, book["mid"], pos.get("side", "YES"))
                _close_position(client, pos, book, "clob_sync_confirmed", pnl_pct)
                continue
            # Si todavía tiene balance real → tratar como posición normal

        book = _fetch_book(client, token_id)
        if not book:
            continue

        entry = float(pos.get("entry_price") or 0.0)
        side_str = (pos.get("side") or "BUY").upper()
        exit_price_now = book["bid"] if side_str == "BUY" else book["ask"]
        pnl_pct = _compute_pnl_pct(entry, exit_price_now, side_str)

        # MIRROR_ONLY_ON_LOSS_V1 (JP 2026-05-05): mirror solo dispara si swisstony
        # vende con pérdida grande (≤-20%). Eso indica evento serio en el mercado
        # (lesión, gol, news). Si vende ganando o perdiendo poco → ignorar y dejar
        # que el profit-runner (trailing +10/-12, SL -35%) maneje la salida.
        # MIRROR_KILLED_V2_2026_05_06 (JP+Opus): mirror sell DESACTIVADO completo.
        # Razón: el 6-may Pigato (+$10) y Carabelli (+$7) cerraron por mirror con
        # partidos vivos, ignorando trailing/SL/TP. JP: "si el partido vive, no salimos
        # por capricho de swisstony". Profit-runner (-35 SL, 0.95 TP, trailing +10/-12)
        # + PANIC_EXIT manejan TODAS las salidas. Reversible: quitar 'False:' línea de abajo.
        mirror_sell = _find_recent_swisstony_sell(pos)
        mirror_qualifies = _mirror_loss_qualifies(pos, mirror_sell) if mirror_sell else False
        if False:
            ok = _close_position(client, pos, book, "swisstony_mirror_sell", pnl_pct)
            try:
                create_record("LogEvent", {
                    "level": "warn",
                    "module": "position_tp_sl",
                    "message": f"SWISSTONY_MIRROR_EXIT: closed {str(pos.get('id'))[:8]} after Swisstony SELL",
                    "data": {
                        "position_id": pos.get("id"),
                        "token_id": str(token_id)[-12:],
                        "market": (pos.get("market") or pos.get("question") or "")[:120],
                        "sell_trade_hash": mirror_sell.get("trade_hash"),
                        "sell_price": mirror_sell.get("price"),
                        "pnl_pct": pnl_pct,
                        "closed_ok": ok,
                    },
                })
            except Exception:
                pass
            if ok:
                try:
                    closed_mirror += 1
                except Exception:
                    pass
            else:
                dust_exits += 1
            continue

        try:
            update_record("Position", pos.get("id"), {
                "current_price": exit_price_now,
                "pnl_unrealized": round(
                    float(pos.get("size_usdc") or 0.0) * pnl_pct, 4
                ),
            })
        except Exception:
            pass

        tp_pct = default_tp
        sl_pct = min(-0.35, float(default_sl))  # PROFIT_RUNNER_35_12_V1: nunca stop mas corto que -35%
        try:
            linked = list_records(
                "CopyTradeProposal",
                limit=1,
                query={"executed_position_id": pos.get("id")},
                sort="-created_date",
            )
            if linked:
                p = linked[0]
                tp_pct = max(0.95, float(p.get("take_profit_pct") or default_tp))  # PROFIT_RUNNER_35_12_V1: ignora TP chico 8/12/35%, profit-runner real
                # BOTCONFIG_OVERRIDES_PROPOSAL_SL_V1: BotConfig.stop_loss manda siempre. Las proposals viejas
                # tienen stop_loss_pct=-0.08 hardcodeado del schema y pisaban el -0.35
                # de BotConfig (caso Arsenal cerró a -8.5%). TP sigue respetando proposal.
                sl_pct = default_sl
        except Exception:
            pass

        # TRAILING_RESPECT_BOTCONFIG_V1 (JP+Opus 2026-05-05): leemos BotConfig
        # en cada loop. Si trailing_stop_enabled=false → SL fijo, sin trailing.
        # Si está enabled → usamos los thresholds de BotConfig (no las constantes
        # hardcodeadas). Caso Arsenal -8% post +16% HWM = trailing actuó cuando JP
        # lo había desactivado en DB pero el bot ignoraba el flag.
        try:
            _bot_cfg_list = list_records("BotConfig", limit=1, sort="-updated_date")
            _bot_cfg = _bot_cfg_list[0] if _bot_cfg_list else {}
        except Exception:
            _bot_cfg = {}
        _trailing_enabled = bool(_bot_cfg.get("trailing_stop_enabled", False))
        _trailing_act = float(_bot_cfg.get("trailing_activation_pct") or TRAILING_ACTIVATION_PCT)
        _trailing_dist = float(_bot_cfg.get("trailing_distance_pct") or TRAILING_DISTANCE_PCT)

        # TRAILING_STOP_V1 + TRAILING_STOP_VAR_FIX_V1: actualizar high-water mark.
        # Lo actualizamos siempre (aunque trailing esté off) para mantener telemetría.
        prev_max = pos.get("max_pnl_pct")
        prev_max_f = float(prev_max) if prev_max is not None else None
        new_max = pnl_pct if (prev_max_f is None or pnl_pct > prev_max_f) else prev_max_f
        if prev_max_f is None or pnl_pct > prev_max_f:
            try:
                update_record("Position", pos.get("id"), {"max_pnl_pct": new_max})
            except Exception as _e:
                logger.debug("trailing: update max_pnl_pct failed pos=%s err=%s",
                             str(pos.get("id", ""))[:8], _e)
        # Decisión del SL efectivo: solo activamos trailing si BotConfig lo permite.
        if _trailing_enabled and new_max is not None and new_max >= _trailing_act:
            effective_sl_pct = new_max - _trailing_dist
            sl_mode = "trailing"
        else:
            effective_sl_pct = sl_pct
            sl_mode = "fixed_botconfig_off" if not _trailing_enabled else "fixed"

        # PANIC_EXIT_V1 — panic exit toma precedencia sobre TP/SL normal.
        # Si pnl <= -30% y age >= 5min, vender al market sin más checks.
        opened_ts_panic = pos.get("opened_at_ts")
        age_seconds_panic = (time.time() - float(opened_ts_panic)) if opened_ts_panic else 0.0
        # PANIC_EXIT_AGE_GUARD_V2 — descartar age fantasma (opened_at_ts roto/ausente)
        # Caso Mumbai vs Sunrisers 2026-04-28: age=15727089s (182 dias) por opened_at_ts
        # roto/ausente disparaba PANIC_EXIT a los 7s de abrir. Bloquear si age es no-confiable.
        _age_invalid = (age_seconds_panic < 0) or (age_seconds_panic > 86400)
        if _age_invalid and pnl_pct <= PANIC_DRAWDOWN_THRESHOLD:
            logger.warning(
                "PANIC_EXIT_AGE_GUARD: pos %s age=%.0fs invalido (timestamp roto), panic_exit BLOQUEADO",
                str(pos.get("id", ""))[:8], age_seconds_panic,
            )
        if (not _age_invalid) and pnl_pct <= PANIC_DRAWDOWN_THRESHOLD and age_seconds_panic >= PANIC_MIN_AGE_SECONDS:
            logger.warning(
                "PANIC_EXIT triggered: pos=%s pnl=%.1f%% age=%.0fs (threshold %.0f%% / %.0fs)",
                str(pos.get("id", ""))[:8], pnl_pct * 100, age_seconds_panic,
                PANIC_DRAWDOWN_THRESHOLD * 100, PANIC_MIN_AGE_SECONDS,
            )
            ok = _close_position(client, pos, book, "panic_exit", pnl_pct)
            if ok:
                closed_sl += 1
            else:
                dust_exits += 1
        elif pnl_pct >= tp_pct:
            # GUARD_TIME_TO_RESOLVE_V1: si faltan >90min para resolución y ya
            # estamos en TP, NO cerramos — dejamos correr para que el trailing
            # capture más ganancia. Solo aplica al TP fijo, no al trailing_stop.
            mins_left = _minutes_to_resolve(pos)
            if mins_left is not None and mins_left > GUARD_TIME_TO_RESOLVE_MIN:
                logger.info(
                    "GUARD_TIME_TO_RESOLVE: skip TP fijo pos=%s pnl=%.1f%% mins_left=%.0f (>%d) — dejando correr trailing",
                    str(pos.get("id", ""))[:8], pnl_pct * 100, mins_left,
                    GUARD_TIME_TO_RESOLVE_MIN,
                )
                continue
            ok = _close_position(client, pos, book, "take_profit", pnl_pct)
            if ok:
                closed_tp += 1
            else:
                dust_exits += 1
        elif pnl_pct <= effective_sl_pct:
            # TRAILING_STOP_V1: si sl_mode=trailing, marcamos close_reason=trailing_stop
            # para distinguir cierres con ganancia vs cierres con pérdida fija.
            close_reason_label = "trailing_stop" if sl_mode == "trailing" else "stop_loss"
            ok = _close_position(client, pos, book, close_reason_label, pnl_pct)
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
        "closed_mirror": closed_mirror,
        "pending_ghost_closed": pending_stats["pending_ghost_closed"],
        "pending_fill_confirmed": pending_stats["pending_fill_confirmed"],
    }
