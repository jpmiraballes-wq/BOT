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

DEFAULT_TP_PCT = 0.12
DEFAULT_SL_PCT = -0.08
# TRAILING_STOP_V1: trailing stop dinámico. Cuando max_pnl_pct (HWM) supera
# TRAILING_ACTIVATION_PCT, el SL efectivo pasa a max_pnl_pct - TRAILING_DISTANCE_PCT.
# Mientras max_pnl_pct < TRAILING_ACTIVATION_PCT se usa el SL fijo (default_sl).
TRAILING_ACTIVATION_PCT = 0.50  # JP+Opus 2026-05-04: solo trailing en trades >=+50%  # +5% para activar
TRAILING_DISTANCE_PCT = 0.10  # JP+Opus 2026-05-04: deja correr 10% desde el pico    # 2¢ debajo del HWM
POLYMARKET_MIN_SHARES = 5.0
AGGRESSIVE_DISCOUNT_USD = 0.02

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
        # TP_SL_MID_DOUBLE_CONFIRM_V1: floor 5¢ BUY / 95¢ SELL (Bolt audit 2026-04-27).
        # Antes podía caer a 1¢ si current_price era basura. No regalamos shares.
        agg_price = (max(0.05, current_price - AGGRESSIVE_DISCOUNT_USD)
                     if side_str == "BUY"
                     else min(0.95, current_price + AGGRESSIVE_DISCOUNT_USD))
        res = _try_close(client, token_id, side_str, shares, agg_price,
                         order_type=OrderType.FAK)
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

    # TP_SL_MID_DOUBLE_CONFIRM_V1: dust_unsellable SIN PnL fantasma (Bolt audit 2026-04-27).
    # Si NO pudimos vender, NO sabemos el precio real de salida.
    # NO inventamos PnL con current_price (puede ser bid roto).
    # Marcamos dust_unsellable con pnl=0 y dejamos shares on-chain.
    # NO creamos Trade record: el resolver lo hara al cerrar el mercado.
    last_err = (res.get("error") or "unknown")[:80]
    update_record("Position", pos_id, {
        "status": "closed",
        "close_reason": "dust_unsellable",
        "close_time": now_ts_iso,
        "pnl_realized": 0.0,
        "pnl_unrealized": 0.0,
        "notes": (pos.get("notes") or "") +
                 f" | dust_unsellable: {last_err} (intended {reason}, "
                 f"shares on-chain hasta resolucion)",
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


def manage_open_positions(client) -> Dict[str, int]:
    """Loop principal. TODAS las strategies."""
    if client is None:
        return {"checked": 0, "closed_tp": 0, "closed_sl": 0, "dust_exits": 0}

    # FIX 2026-04-26 JP: monitorea TODAS las strategies (antes solo whale_consensus).
    positions = list_records("Position", limit=50, query={"status": "open"}, sort="-opened_at")
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

        tp_pct = default_tp
        sl_pct = default_sl
        try:
            linked = list_records(
                "CopyTradeProposal",
                limit=1,
                query={"executed_position_id": pos.get("id")},
                sort="-created_date",
            )
            if linked:
                p = linked[0]
                tp_pct = float(p.get("take_profit_pct") or default_tp)
                # BOTCONFIG_OVERRIDES_PROPOSAL_SL_V1: BotConfig.stop_loss manda siempre. Las proposals viejas
                # tienen stop_loss_pct=-0.08 hardcodeado del schema y pisaban el -0.35
                # de BotConfig (caso Arsenal cerró a -8.5%). TP sigue respetando proposal.
                sl_pct = default_sl
        except Exception:
            pass

        # TRAILING_STOP_REORDER_V1: bloque trailing reubicado acá. Necesita sl_pct ya definida
        # (línea 526 + override 537). Va antes de PANIC_EXIT para que el elif
        # pnl_pct <= effective_sl_pct (línea ~571) tenga ambas variables listas.
        # TRAILING_STOP_V1 + TRAILING_STOP_VAR_FIX_V1: actualizar high-water mark y calcular SL efectivo.
        prev_max = pos.get("max_pnl_pct")
        prev_max_f = float(prev_max) if prev_max is not None else None
        new_max = pnl_pct if (prev_max_f is None or pnl_pct > prev_max_f) else prev_max_f
        if prev_max_f is None or pnl_pct > prev_max_f:
            try:
                update_record("Position", pos.get("id"), {"max_pnl_pct": new_max})
            except Exception as _e:
                logger.debug("trailing: update max_pnl_pct failed pos=%s err=%s",
                             str(pos.get("id", ""))[:8], _e)
        if new_max is not None and new_max >= TRAILING_ACTIVATION_PCT:
            effective_sl_pct = new_max - TRAILING_DISTANCE_PCT
            sl_mode = "trailing"
        else:
            effective_sl_pct = sl_pct
            sl_mode = "fixed"

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
    }
