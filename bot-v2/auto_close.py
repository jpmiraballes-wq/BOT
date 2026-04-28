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

CLOB_PRICE_URL = "https://clob-v2.polymarket.com/price"
REQUEST_TIMEOUT = 8
MAX_FAIL_ATTEMPTS = 3
_FAIL_COUNTS = {}
# DEDUP_REENTRY_V1: pos_ids cerrados recientemente (epoch timestamp).
# Previene duplicar Trades cuando list_records devuelve la misma pos
# dos veces por latencia de Base44 en el PUT de status=closed.
import time as _t_dedup
_CLOSED_RECENTLY = {}
_DEDUP_TTL_SEC = 300  # 5 minutos

def _was_closed_recently(pos_id):
    now = _t_dedup.time()
    # cleanup old entries
    stale = [k for k, ts in _CLOSED_RECENTLY.items() if now - ts > _DEDUP_TTL_SEC]
    for k in stale:
        _CLOSED_RECENTLY.pop(k, None)
    return pos_id in _CLOSED_RECENTLY

def _mark_closed_recently(pos_id):
    _CLOSED_RECENTLY[pos_id] = _t_dedup.time()


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
    # TOKEN_ID_FIX_V1: antes se pasaba pos.get("id") (ID de Base44)
    # como primer argumento, y close_position_market lo mandaba a
    # /balance-allowance como asset_id => Polymarket 400 "assetId invalid".
    # El primer argumento DEBE ser el token_id real de Polymarket (numero
    # gigante de 70+ digitos). Si no hay token_id, no podemos cerrar live.
    token_id = pos.get("token_id")
    if not token_id:
        logger.warning("close: posicion %s sin token_id, no se puede cerrar live",
                       str(pos.get("id"))[:10])
        return None
    shares = _safe_float(pos.get("size_tokens")) or _safe_float(pos.get("size_usdc"))
    
    try:
        sig = inspect.signature(om.close_position_market)
        params = sig.parameters
    except (ValueError, TypeError):
        # Fallback: sin introspeccion, intentar sin kwarg de cantidad.
        return om.close_position_market(token_id)
    
    # Construir kwargs segun lo que acepta la funcion
    kwargs = {}
    if "shares" in params and shares is not None:
        kwargs["shares"] = shares
    elif "size" in params and shares is not None:
        kwargs["size"] = shares
    # Si no tiene ninguno, simplemente no pasamos cantidad
    
    return om.close_position_market(token_id, **kwargs)


def _close_position(om, pos, pnl_pct, reason, current_price):
    pos_id = pos.get("id")
    if not pos_id:
        return False

    # DEDUP_REENTRY_V1: si ya se cerro esta pos en los ultimos 5 min,
    # saltar para no crear Trade duplicado. Esto bloquea el race
    # condition con la latencia del write de Base44.
    if _was_closed_recently(pos_id):
        logger.info("AutoClose: pos=%s cerrada recientemente, skip reentry", pos_id[-8:])
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
        _mark_closed_recently(pos_id)
        _settle_copy_trade_accounting(pos, pos_id, reason, current_price, pnl_pct)  # PNL_ACCOUNTING_V1
        return True

    # --- Live mode: intentar cierre real ---
    # DUST_ZOMBIE_V1: antes de intentar el close, chequeamos balance
    # on-chain. Si balance < 5 tokens, es dust unsellable (el BUY
    # original nunca se lleno o se lleno parcial minimo). En vez de
    # gastar 3 reintentos FOK-fail, marcamos la posicion closed
    # directamente con close_reason=dust_unsellable y Trade pnl=0.
    # DUST_ZOMBIE_V2: leemos balance via llamada HTTP directa a la API
    # publica de Polymarket (clob.polymarket.com/balance-allowance) en vez
    # de om.client.get_balance_allowance que tiraba excepciones silenciosas.
    # Tambien usamos logger.warning (no debug) para ver fallos del check.
    # TOKEN_ID_UNBOUND_FIX_V1: asegurar token_id definido antes de cualquier
    # rama condicional. Sin esto, Python lo trata como local y tira
    # UnboundLocalError cuando strategy != whale_consensus.
    token_id = pos.get("token_id")

    # WHALE_CONSENSUS_GRACE_V1 + REINDENT_GRACE_V2: las Positions copy-trade
    # recien creadas necesitan >2min para que copy_executor complete el fill via FAK.
    # Si marcamos dust_unsellable antes, perdemos la oportunidad de retry.
    if pos.get("strategy") == "whale_consensus":
        import time as _t
        opened_ts = pos.get("opened_at_ts") or 0
        if opened_ts and (_t.time() - float(opened_ts)) < 120:
            logger.info("AutoClose SKIP whale_consensus <120s old: pos=%s", pos_id[-8:] if pos_id else "?")
            return False
    if token_id:
        try:
            bal_url = "https://clob-v2.polymarket.com/balance-allowance"
            bal_params = {
                "asset_type": "CONDITIONAL",
                "token_id": str(token_id),
                "signature_type": 2,
            }
            # Nota: esta ruta tambien existe autenticada pero para solo
            # lectura del propio wallet funciona con la proxy address que
            # el order_manager ya configuro. Si no, om.client lo hace.
            # DUST_ZOMBIE_V3: get_balance_allowance espera BalanceAllowanceParams
            # (objeto), no dict. Importamos la clase y construimos el param.
            bal_resp = None
            if hasattr(om, "client") and hasattr(om.client, "get_balance_allowance"):
                try:
                    from _clob_compat import BalanceAllowanceParams, AssetType
                    _bap = BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=str(token_id),
                        signature_type=2,
                    )
                    bal_resp = om.client.get_balance_allowance(_bap)
                except Exception as _bap_exc:
                    logger.warning(
                        "DUST_ZOMBIE_V3: BalanceAllowanceParams fallo (%s): %s",
                        pos_id[-8:], _bap_exc,
                    )
            raw_bal = (bal_resp or {}).get("balance", "0")
            # El API devuelve balance en unidades de 1e6 (USDC-style).
            # Si raw_bal es string "1066500" -> 1.0665 tokens reales.
            on_chain_tokens = float(raw_bal) / 1_000_000.0
            logger.info(
                "AutoClose balance check pos=%s token=%s raw=%s tokens=%.4f",
                pos_id[-8:], str(token_id)[:10], raw_bal, on_chain_tokens,
            )
            if on_chain_tokens < 5.0:
                logger.warning(
                    "AutoClose DUST: pos=%s balance=%.4f < 5 tokens -> closing direct (dust_unsellable)",
                    pos_id[-8:], on_chain_tokens,
                )
                # NO_DUST_TRADE_V1: solo cerramos la Position. NO
                # creamos Trade porque el trade nunca ocurrio (la orden
                # BUY original nunca se lleno). Crear un Trade con pnl=0
                # solo ensuciaba stats, equity y dashboard.
                update_record("Position", pos_id, {
                    "status": "closed",
                    "close_time": _iso_now(),
                    "close_reason": "dust_unsellable",
                    "current_price": current_price,
                    "pnl_unrealized": 0,
                    "notes": f"dust_unsellable: on_chain_balance={on_chain_tokens:.4f} tokens (BUY never filled)",
                })
                _FAIL_COUNTS.pop(pos_id, None)
                return True
        except Exception as exc:
            logger.warning("AutoClose DUST check fallo (%s): %s", pos_id[-8:], exc)

    # NONE_IS_FAIL_V1: antes la logica era "None = True (exito)", al
    # reves. close_position_market devuelve None cuando hay fallo
    # silencioso (balance 0, API error manejado, skip por token_id
    # faltante). Eso produjo 13 trades fantasma en 1 minuto el 21-abr.
    # Ahora: solo consideramos ok si result es truthy (dict con order_id,
    # hash, o True). None/False/{}/[] = fallo -> NO se crea Trade.
    try:
        result = _call_close_market(om, pos)
        ok = bool(result)
        if not ok:
            logger.warning(
                "close live sin confirmacion (%s, reason=%s, result=%r) -> NO crear Trade",
                pos_id[-8:], reason, result,
            )
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
        _mark_closed_recently(pos_id)
        _FAIL_COUNTS.pop(pos_id, None)
        _settle_copy_trade_accounting(pos, pos_id, reason, current_price, pnl_pct)  # PNL_ACCOUNTING_V1
        return True

    # Cierre live fallo -> incrementar contador, NO crear Trade
    _FAIL_COUNTS[pos_id] = _FAIL_COUNTS.get(pos_id, 0) + 1
    attempts = _FAIL_COUNTS[pos_id]
    logger.warning("AutoClose: pos=%s cierre live fallo (%d/%d). No se crea Trade.",
                   pos_id[-8:], attempts, MAX_FAIL_ATTEMPTS)

    if attempts >= MAX_FAIL_ATTEMPTS:
        # NO_ORPHAN_TRADE_V1: cuando close live falla 3x, NO creamos Trade
        # fantasma con pnl=0 (ensuciaba stats historicos). Tampoco marcamos
        # la Position como closed, porque la posicion SIGUE ABIERTA on-chain.
        # Reseteamos el contador y dejamos que AutoClose la reintente en
        # el proximo ciclo. Si nunca cierra, el user la cierra manual.
        logger.warning(
            "AutoClose: pos=%s cierre fallo %dx. Position queda abierta, "
            "se reintentara proximo ciclo. NO se crea Trade fantasma.",
            pos_id[-8:], MAX_FAIL_ATTEMPTS,
        )
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
        # SKIP_ORPHANED_V1: si la posicion fue marcada orphan en un ciclo
        # anterior pero volvio a estar "open" (probablemente porque
        # syncPositionsWithWallet la re-importo desde la wallet real),
        # no reintentamos cerrarla. Evita el loop que genero 75 trades
        # duplicados el 21-apr-2026.
        # DUST_LOOP_FIX_V1: tambien saltamos dust_unsellable. Si el
        # AutoClose ya marco la posicion como dust antes, no tiene sentido
        # re-intentarla cada 2 min solo porque syncPositionsWithWallet la
        # re-importo como open (el balance on-chain existe pero es dust).
        if pos.get("close_reason") in ("unverified_orphan", "dust_unsellable"):
            continue

        # MIDPOINT_FALLBACK_V1 (2026-04-22): si el book del CLOB esta
        # roto (sin bid/ask o cruzado), _fetch_midpoint devuelve None y
        # antes saltabamos la posicion -> NUNCA disparaba SL. Ahora
        # caemos al current_price guardado por position_tracker. Si
        # tampoco hay, usamos entry_price (PnL=0, no cierra, pero al
        # menos no bloquea el loop). Esto permitio que Monero llegara
        # a -18% sin stop-loss.
        current = _fetch_midpoint(token_id)
        if current is None:
            current = _safe_float(pos.get("current_price"))
            if current is None or current <= 0:
                logger.warning(
                    "AutoClose: pos=%s sin midpoint ni current_price, skip",
                    str(pos.get("id"))[-8:],
                )
                continue
            logger.info(
                "AutoClose: pos=%s book CLOB roto, usando current_price=%.4f",
                str(pos.get("id"))[-8:], current,
            )

        # Phantom guard: descartar PnL irreal
        pnl_pct = _compute_pnl_pct(pos, current)
        if pnl_pct is None or pnl_pct > 5.0 or pnl_pct < -0.95:
            continue

        checked += 1

        # BREAKEVEN_TRAILING_V1: si ganamos >=+6%, subir stop_loss_price a entry_price (breakeven lock).
        # Esto NO cierra la posicion, solo blinda contra dar la vuelta. Idempotente:
        # si ya tiene SL en breakeven (o mejor), no toca nada.
        try:
            if pnl_pct >= 0.06:
                cur_sl = _safe_float(pos.get("stop_loss_price"))
                if cur_sl is None or cur_sl < entry:
                    pos_id_be = pos.get("id")
                    update_record("Position", pos_id_be, {"stop_loss_price": float(entry)})
                    logger.info(
                        "sl_moved_to_breakeven pos=%s entry=%.4f pnl_pct=%.4f",
                        str(pos_id_be)[-8:], entry, pnl_pct,
                    )
        except Exception as _be_err:
            logger.warning("breakeven_trailing_failed: %s", _be_err)

        reason = None
        # FIX #2 (2026-04-23): Sell anticipado al +25% sin esperar resoluciÃÂ³n.
        # Si la posiciÃÂ³n subiÃÂ³ 25% desde entrada, lockeamos ganancia aunque
        # el take_profit configurado sea mayor. Evita devolver profit al mercado.
        if pnl_pct >= 0.25:
            reason = "early_profit_exit"
        elif pnl_pct >= take_profit:
            reason = "take_profit"
        elif pnl_pct <= stop_loss:
            reason = "stop_loss"

        if reason:
            if _close_position(om, pos, pnl_pct, reason, current):
                closed += 1

    mode = "paper" if DRY_RUN else "live"
    logger.info("AutoClose: tp=%s sl=%s checked=%d closed=%d mode=%s",
                take_profit, stop_loss, checked, closed, mode)


# --------------------------------------------------------------------------
# Wrapper de compatibilidad: main.py espera una clase AutoClose(om) con un
# metodo check_and_close(). Mantenemos la funcion standalone y agregamos la
# clase como thin wrapper para no romper la API historica.
# --------------------------------------------------------------------------
class AutoClose:
    """Thin wrapper sobre check_and_close() para compat con main.py."""

    def __init__(self, om=None):
        self.om = om

    def check_and_close(self):
        return check_and_close(self.om)

    # main.py historico llama aclose.run(). Mantenemos el alias para
    # compat, apunta exactamente a check_and_close.
    def run(self):
        return check_and_close(self.om)


# ============================================================================
# PNL_ACCOUNTING_V1 (2026-04-24): contabilidad correcta al cerrar posiciones
# ============================================================================
def _settle_copy_trade_accounting(pos, pos_id, reason, current_price, pnl_pct):
    """Cierra correctamente una Position:
       - escribe pnl_realized + exit_price en Position
       - si strategy=whale_consensus: actualiza WhaleCopyWallet (deployed/wins/losses/pnl_total)
       - si hay CopyTradeProposal linkeada: set proposal.pnl
    """
    try:
        # PNL_ACCOUNTING_V2: distinguir fills reales de rebotes
        entry = _safe_float(pos.get("entry_price")) or 0.0
        size = _safe_float(pos.get("size_usdc")) or 0.0
        reason_lc = str(reason or "").lower()
        _UNFILLED_REASONS = {"fak_no_fill", "dust_unsellable", "cancelled_no_fill", "gtc_timeout_no_fill"}
        is_unfilled = (
            reason_lc in _UNFILLED_REASONS
            or reason_lc.startswith("price_drifted")
            or reason_lc.startswith("stale_price_drift")
            or reason_lc.startswith("rejected")
            or reason_lc.startswith("expired_")
        )
        pnl_realized = 0.0 if is_unfilled else round(size * (pnl_pct or 0.0), 4)

        # 1) Update Position con pnl_realized + exit_price (patch sobre el update ya hecho)
        update_record("Position", pos_id, {
            "pnl_realized": pnl_realized,
            "exit_price": current_price,
        })

        # 2) Si es whale_consensus -> actualizar WhaleCopyWallet
        if pos.get("strategy") == "whale_consensus":
            try:
                wallets = list_records("WhaleCopyWallet", {}, limit=1)
                if wallets:
                    w = wallets[0]
                    wid = w.get("id")
                    deployed = _safe_float(w.get("deployed_usdc")) or 0.0
                    pnl_total = _safe_float(w.get("pnl_total_usdc")) or 0.0
                    pnl_today = _safe_float(w.get("pnl_today_usdc")) or 0.0
                    wins = int(_safe_float(w.get("wins")) or 0)
                    losses = int(_safe_float(w.get("losses")) or 0)

                    new_deployed = max(0.0, round(deployed - size, 2))
                    new_pnl_total = round(pnl_total + pnl_realized, 4)
                    new_pnl_today = round(pnl_today + pnl_realized, 4)
                    if not is_unfilled:
                        if pnl_realized > 0:
                            wins += 1
                        elif pnl_realized < 0:
                            losses += 1

                    update_record("WhaleCopyWallet", wid, {
                        "deployed_usdc": new_deployed,
                        "pnl_total_usdc": new_pnl_total,
                        "pnl_today_usdc": new_pnl_today,
                        "wins": wins,
                        "losses": losses,
                    })
                    logger.info(
                        "PNL_ACCOUNTING_V1 wallet update: pos=%s pnl=%.2f deployed=%.2f->%.2f",
                        pos_id[-8:], pnl_realized, deployed, new_deployed,
                    )
            except Exception as exc:
                logger.warning("PNL_ACCOUNTING_V1 wallet update fallo: %s", exc)

            # 3) CopyTradeProposal linkeada (pos_id == proposal.executed_position_id)
            try:
                proposals = list_records(
                    "CopyTradeProposal",
                    {"executed_position_id": pos_id},
                    limit=1,
                )
                if proposals:
                    prop = proposals[0]
                    update_record("CopyTradeProposal", prop.get("id"), {
                        "pnl": pnl_realized,
                    })
            except Exception as exc:
                logger.warning("PNL_ACCOUNTING_V1 proposal update fallo: %s", exc)
    except Exception as exc:
        logger.warning("PNL_ACCOUNTING_V1 fallo general: %s (pos=%s)", exc, pos_id[-8:] if pos_id else "?")

