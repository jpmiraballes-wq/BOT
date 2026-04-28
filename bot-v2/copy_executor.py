"""copy_executor.py - Ejecutor profesional de copy-trade whale_consensus.

Reemplaza la vieja drain_pending_fills() que era monolitica y fragil.

FEATURES:
  - Fix notional 2dp (bug actual Polymarket)
  - Tick size dinamico por mercado
  - Retry con backoff exponencial (errores transitorios)
  - Poll fill status 3x con 500ms delay (FAK lag)
  - Min notional $5 real
  - Balance check USDC previo
  - Pending timeout 5min (auto-expira Positions zombis)
  - Clasificacion error: solo cierra Position si rechazo definitivo
  - Dedup Telegram (misma alerta no se manda 2x)
  - Log estructurado a Base44 (LogEvent)

USO:
  from copy_executor import CopyExecutor
  executor = CopyExecutor(clob_client, funder_address)
  processed = executor.drain()  # llamar cada loop iter
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

import os
import polymarket_api as pmapi
from base44_client import list_records, update_record
from decision_logger import log_decision, log_warning, log_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
PENDING_TIMEOUT_SEC = 300.0            # 5 minutos -> auto-expire
MAX_RETRIES = 3                        # reintentos ante errores transitorios
RETRY_BACKOFF_BASE = 1.5               # 1.5s, 2.25s, 3.4s entre reintentos
FILL_POLL_ATTEMPTS = 3                 # polls post-FAK para confirmar matching
FILL_POLL_DELAY_MS = 500               # delay entre polls
MIN_NOTIONAL_USDC = 5.0                # minimo real de Polymarket
PRICE_SLIPPAGE_TICKS = 2               # GTC_5MIN_V1: +2 ticks (2c) para mejor fill rate
GTC_FILL_TIMEOUT_SEC = 300.0           # GTC_5MIN_V1: orden viva hasta 5min antes de cancelar
MAX_PRICE_DRIFT_PCT = 0.20             # si precio se movio >20% desde proposal, abortar

# Dedup de alertas Telegram (en memoria, se resetea al reiniciar bot)
_ALERT_HISTORY: set = set()


def _send_telegram(html: str) -> None:
    """Envia mensaje HTML a Telegram usando env vars. Best-effort.

    Usa TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (mismo patron que telegramNotify).
    Si faltan env vars o la API falla, loggea y sigue -> no rompe el bot.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        logger.debug("Telegram: vars faltantes, skip")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat, "text": html,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code >= 400:
            logger.warning("Telegram HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        logger.warning("Telegram send fallo: %s", exc)


def _alert_once(key: str, html: str) -> bool:
    """Manda alerta Telegram solo si no se envio antes (key unica).
    Devuelve True si mando, False si ya existia.
    """
    if key in _ALERT_HISTORY:
        return False
    _ALERT_HISTORY.add(key)
    _send_telegram(html)
    return True


# ---------------------------------------------------------------------------
# Clase principal
# ---------------------------------------------------------------------------
class CopyExecutor:
    """Ejecutor de Positions pending_fill (copy-trade de Telegram)."""

    def __init__(self, clob_client, funder_address: str = "") -> None:
        self.client = clob_client
        self.funder = funder_address or ""

    # -------------------------------------------------------------- entry
    def drain(self) -> int:
        """Procesa todas las Positions pending_fill pendientes.

        Returns:
            Cantidad total de Positions procesadas (fill + fail + expired).
        """
        if self.client is None:
            logger.warning("CopyExecutor.drain: clob_client es None")
            return 0

        try:
            pending = self._fetch_pending()
        except Exception as exc:
            logger.error("drain: no pude listar pending Positions: %s", exc)
            return 0

        if not pending:
            return 0

        logger.info("drain: procesando %d Positions pending_fill", len(pending))

        # Log estructurado a Base44 para visibilidad en dashboard
        log_decision(
            reason="drain_batch_start",
            market="batch",
            strategy="whale_consensus",
            extra={"pending_count": len(pending)},
        )

        processed = 0
        for pos in pending:
            try:
                if self._execute_one(pos):
                    processed += 1
            except Exception as exc:
                # Defensivo: un pos con bug no debe romper el batch entero
                logger.exception("drain: excepcion procesando pos=%s: %s",
                                 pos.get("id"), exc)
                log_error(
                    "drain_position_unexpected_error",
                    module="copy_executor",
                    extra={"pos_id": pos.get("id"), "error": str(exc)[:200]},
                )

        logger.info("drain: terminado, %d/%d procesadas", processed, len(pending))
        return processed

    # --------------------------------------------------------- fetch list
    def _fetch_pending(self) -> List[Dict[str, Any]]:
        """Lee Positions con pending_fill=true y status=open."""
        # list_records(entity, sort, limit) ÃÂ¢ÃÂÃÂ sin filtros server-side
        # filtramos en Python para mantener la firma publica simple.
        records = list_records(
            "Position",
            sort="-created_date",
            limit=100,
        ) or []

        pending = []
        for rec in records:
            # list_records puede devolver dict anidado en 'data'
            data = rec.get("data") if isinstance(rec.get("data"), dict) else rec
            if not data:
                continue
            if data.get("status") != "open":
                continue
            if data.get("strategy") != "whale_consensus":
                continue
            if not data.get("pending_fill"):
                continue
            pid = rec.get("id") or data.get("id")
            if not pid:
                continue
            # Inyectamos el id en el data para facilitar manejo
            data["id"] = pid
            pending.append(data)
        return pending

    # ------------------------------------------------------- execute one
    def _execute_one(self, pos: Dict[str, Any]) -> bool:
        """Ejecuta UNA Position pending_fill. Devuelve True si se proceso."""
        pos_id = pos.get("id")
        token_id = pos.get("token_id")
        side_str = (pos.get("side") or "BUY").upper()
        size_usdc = float(pos.get("size_usdc") or 0.0)
        market_label = (pos.get("question") or pos.get("market") or "?")[:60]
        # Prioridad: opened_at (ISO string fresca) > created_date > opened_at_ts (epoch, a veces descuadrado).
        # Si todo falla, asumimos que la Position es nueva (age=0) para no expirar mal.
        created_ts = None
        for key in ("opened_at", "created_date"):
            raw = pos.get(key)
            if not raw:
                continue
            if isinstance(raw, str):
                try:
                    created_ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
                    break
                except Exception:
                    continue
            elif isinstance(raw, (int, float)) and raw > 0:
                created_ts = float(raw)
                break

        # ÃÂÃÂltimo recurso: opened_at_ts (puede venir con offset horario raro).
        if created_ts is None:
            ts_raw = pos.get("opened_at_ts")
            if isinstance(ts_raw, (int, float)) and ts_raw > 0:
                created_ts = float(ts_raw)

        # Si aÃÂÃÂºn no tenemos nada, tratamos como reciÃÂÃÂ©n creada.
        if created_ts is None:
            created_ts = time.time()

        age_sec = max(0.0, time.time() - created_ts)

        # ---- Gate 1: timeout de 5min ----
        if age_sec > PENDING_TIMEOUT_SEC:
            self._mark_expired(pos_id, market_label, age_sec)
            return True

        # ---- Gate 2: validaciones basicas ----
        if not pos_id or not token_id or size_usdc <= 0:
            log_warning(
                "copy_invalid_position",
                module="copy_executor",
                extra={"pos_id": pos_id, "token_id": bool(token_id), "size": size_usdc},
            )
            self._mark_closed(pos_id, "invalid_position_data", market_label)
            return True

        # ---- DEDUP_TOKEN_ID_V1 ----
        # Si ya hay OTRA Position abierta sobre el mismo token_id, abortar.
        # El balance on-chain es compartido (ERC1155), y si abrimos varias,
        # al cerrar una rompemos las otras con "not enough balance".
        try:
            sibling_open = list_records(
                "Position",
                filter={"token_id": token_id, "status": "open"},
                limit=10,
            ) or []
            sibling_open = [p for p in sibling_open if p.get("id") != pos_id]
            if sibling_open:
                other_id = (sibling_open[0].get("id") or "")[-8:]
                log_warning(
                    "copy_duplicate_token",
                    module="copy_executor",
                    extra={
                        "pos_id": pos_id,
                        "token_id": token_id[-12:] if token_id else "?",
                        "siblings_open": len(sibling_open),
                        "other_id_tail": other_id,
                    },
                )
                self._mark_closed(
                    pos_id,
                    f"duplicate_token_id (already open: {other_id})",
                    market_label,
                )
                _alert_once(
                    f"dup_token:{pos_id}",
                    f"WARN <b>Copy-trade abortado - token duplicado</b>\n"
                    f"{market_label}\n"
                    f"Ya hay {len(sibling_open)} Position abierta sobre este token. "
                    f"Evitando conflicto de balance on-chain.",
                )
                return True
        except Exception as exc:  # pragma: no cover
            # Si falla el dedup query, seguimos (mejor abrir que perder la senal).
            logger.warning("dedup_token_check_failed pos=%s err=%s", pos_id, exc)

        # ---- Gate 3: balance USDC (solo BUY) ----
        if side_str == "BUY":
            usdc_avail = pmapi.check_usdc_balance(self.client, self.funder)
            if usdc_avail is not None and usdc_avail < size_usdc:
                log_warning(
                    "copy_insufficient_usdc",
                    module="copy_executor",
                    extra={"pos_id": pos_id, "needed": size_usdc, "have": usdc_avail},
                )
                self._mark_closed(pos_id, f"no_usdc ({usdc_avail:.2f}<{size_usdc:.2f})",
                                  market_label)
                _alert_once(
                    f"usdc_low:{pos_id}",
                    f"ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ <b>Sin USDC suficiente</b>\n{market_label}\n"
                    f"Necesita ${size_usdc:.2f}, hay ${usdc_avail:.2f}",
                )
                return True

        # ---- Gate 4: orderbook + precio ----
        best_bid, best_ask = pmapi.best_bid_ask(token_id)
        if best_bid is None and best_ask is None:
            log_warning(
                "copy_empty_book",
                module="copy_executor",
                extra={"pos_id": pos_id, "token_id": token_id[:12]},
            )
            # No cerramos: libro vacio puede ser transitorio. Retry en prox ciclo.
            return False

        tick = pmapi.get_tick_size(token_id)

        # ---- STALE_PRICE_GUARD_V1 ----
        # Si el libro se alejÃÂ³ >25% del entry_price original (que viene del
        # proposal = precio al que las ballenas compraron), abortamos.
        # No tiene sentido "copiar" a un whale a un precio completamente
        # distinto Ã¢ÂÂ es otro trade.
        entry_original = float(pos.get("entry_price") or 0.0)
        mid_now = None
        if best_bid is not None and best_ask is not None:
            mid_now = (float(best_bid) + float(best_ask)) / 2.0
        elif best_ask is not None:
            mid_now = float(best_ask)
        elif best_bid is not None:
            mid_now = float(best_bid)

        if entry_original > 0 and mid_now is not None:
            drift = abs(mid_now - entry_original) / entry_original
            if drift > 0.25:
                log_warning(
                    "copy_stale_price_abort",
                    module="copy_executor",
                    extra={
                        "pos_id": pos_id,
                        "entry_original": entry_original,
                        "mid_now": mid_now,
                        "drift_pct": round(drift * 100, 1),
                    },
                )
                self._mark_closed(
                    pos_id,
                    f"stale_price_drift_{int(drift * 100)}pct",
                    market_label,
                )
                _alert_once(
                    f"stale_price:{pos_id}",
                    f"WARN <b>Copy-trade abortado - precio stale</b>\n"
                    f"{market_label}\n"
                    f"Whales @ {entry_original:.3f} - Libro ahora {mid_now:.3f} "
                    f"(drift {drift*100:.1f}%)",
                )
                return True

        # Precio agresivo: cruzamos el spread por 1 tick para asegurar fill
        if side_str == "BUY":
            base_price = best_ask if best_ask is not None else best_bid
            limit_price = pmapi.round_price_to_tick(
                (base_price or 0.99) + PRICE_SLIPPAGE_TICKS * tick, tick
            )
        else:
            base_price = best_bid if best_bid is not None else best_ask
            limit_price = pmapi.round_price_to_tick(
                max(0.01, (base_price or 0.01) - PRICE_SLIPPAGE_TICKS * tick), tick
            )

        # ---- Gate 4.5: slippage guard (precio se movio vs proposal) ----
        proposal_entry = float(pos.get("entry_price") or 0.0)
        if proposal_entry > 0 and limit_price > 0:
            drift = abs(limit_price - proposal_entry) / proposal_entry
            if drift > MAX_PRICE_DRIFT_PCT:
                log_warning(
                    "copy_price_drifted",
                    module="copy_executor",
                    extra={"pos_id": pos_id, "proposal": proposal_entry,
                           "current": limit_price, "drift_pct": round(drift, 3)},
                )
                self._mark_closed(
                    pos_id,
                    f"price_drifted ({proposal_entry:.3f}->{limit_price:.3f}, {drift*100:.0f}%)",
                    market_label,
                )
                _alert_once(
                    f"drift:{pos_id}",
                    f"\u26a0\ufe0f <b>Copy-trade abortado</b>\n{market_label}\n"
                    f"Precio se movio de <b>{proposal_entry:.3f}</b> a <b>{limit_price:.3f}</b> "
                    f"({drift*100:.0f}%). El bot NO compra a precio muy distinto del aprobado.",
                )
                return True

        # ---- Gate 5: size shares con notional 2dp ----
        size_shares = pmapi.compute_order_size(size_usdc, limit_price, MIN_NOTIONAL_USDC)
        if not size_shares or size_shares <= 0:
            log_warning(
                "copy_size_too_small",
                module="copy_executor",
                extra={"pos_id": pos_id, "size_usdc": size_usdc, "price": limit_price},
            )
            self._mark_closed(pos_id, "size_below_min_notional", market_label)
            _alert_once(
                f"small:{pos_id}",
                f"ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ <b>Copy-trade saltado</b>\n{market_label}\n"
                f"Size ${size_usdc:.2f} insuficiente con price {limit_price:.3f}",
            )
            return True

        # ---- Execute con retry ----
        log_decision(
            reason="copy_fill_start",
            market=market_label,
            strategy="whale_consensus",
            size=size_usdc,
            extra={
                "pos_id": pos_id, "side": side_str, "price": limit_price,
                "shares": size_shares, "tick": tick, "age_sec": round(age_sec, 1),
            },
        )

        order_id, filled_shares, error = self._place_with_retry(
            token_id, side_str, limit_price, size_shares, market_label
        )

        # ---- Resultado ----
        if order_id and filled_shares > 0:
            filled_usdc = round(filled_shares * limit_price, 2)
            self._mark_filled(pos_id, order_id, limit_price, filled_shares,
                              filled_usdc, market_label, side_str)
            return True

        if order_id and filled_shares == 0:
            # GTC_5MIN_V1: orden aceptada sin match inmediato. Esperamos hasta 5min.
            filled_shares_late, fill_err = self._wait_gtc_fill_or_cancel(
                order_id, GTC_FILL_TIMEOUT_SEC, market_label, pos_id
            )
            if filled_shares_late > 0:
                filled_usdc = round(filled_shares_late * limit_price, 2)
                self._mark_filled(pos_id, order_id, limit_price, filled_shares_late,
                                  filled_usdc, market_label, side_str)
                return True
            # No lleno tras 5min -> ya cancelada por _wait_gtc_fill_or_cancel
            self._mark_closed(pos_id, "gtc_timeout_no_fill", market_label)
            _alert_once(
                f"nofill:{pos_id}",
                f"ÃÂ¢ÃÂÃÂ ÃÂ¯ÃÂ¸ÃÂ <b>Copy-trade no llenÃÂÃÂ³</b>\n{market_label}\n"
                f"GTC @ {limit_price:.3f} viva 5min, sin match. Orden cancelada.",
            )
            return True

        # Error definitivo tras retries
        kind = pmapi.classify_error(error) if error else "unknown"
        if kind == "rejected" or kind == "auth":
            self._mark_closed(pos_id, f"rejected:{str(error)[:80]}", market_label)
            _alert_once(
                f"rej:{pos_id}",
                f"ÃÂ¢ÃÂÃÂ <b>Copy-trade rechazado</b>\n{market_label}\n"
                f"Motivo: <code>{str(error)[:150]}</code>",
            )
            return True

        # Transitorio: NO cerramos, esperamos siguiente ciclo (dentro del timeout)
        logger.warning(
            "drain pos=%s error transitorio, reintentara: %s",
            pos_id, str(error)[:120],
        )
        return False

    # ------------------------------------------------- place with retry
    def _place_with_retry(
        self, token_id: str, side_str: str, limit_price: float,
        size_shares: float, market_label: str,
    ) -> Tuple[Optional[str], float, Optional[Exception]]:
        """Envia orden FAK con retry ante errores transitorios.

        Returns: (order_id, filled_shares, last_error)
        """
        side_const = BUY if side_str == "BUY" else SELL
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                args = OrderArgs(
                    token_id=token_id, price=limit_price,
                    size=size_shares, side=side_const,
                )
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC) or {}  # GTC_5MIN_V1
                order_id = resp.get("orderID") or resp.get("orderId")
                status = resp.get("status")

                if status == "rejected" or (resp.get("error") and not order_id):
                    err_msg = str(resp.get("error") or status or "rejected_unknown")
                    last_error = RuntimeError(f"rejected:{err_msg}")
                    # ORDER_VERSION_RETRY_V1: order_version_mismatch es transient (libro se
                    # movio entre firma y envio). Retry inmediato re-firmando.
                    if "order_version_mismatch" in err_msg.lower():
                        log_warning(
                            "order_version_mismatch_retry",
                            module="copy_executor",
                            extra={
                                "attempt": attempt,
                                "max_retries": MAX_RETRIES,
                                "token_id": (token_id or "")[-12:],
                                "side": side_str,
                                "price": limit_price,
                                "shares": size_shares,
                                "err": err_msg[:160],
                            },
                        )
                        if attempt < MAX_RETRIES:
                            continue  # re-loop: re-firma OrderArgs con tick fresh
                        # Agotamos retries
                        return (None, 0.0, last_error)
                    # Rechazo explicito (no version mismatch): no tiene sentido retry
                    return (None, 0.0, last_error)

                if not order_id:
                    last_error = RuntimeError(f"no_order_id:{resp}")
                    # Sin order_id pero sin rechazo -> transient, retry
                    time.sleep(RETRY_BACKOFF_BASE ** attempt)
                    continue

                # Poll fill status
                filled = self._poll_fill(order_id, size_shares)
                return (order_id, filled, None)

            except Exception as exc:
                last_error = exc
                kind = pmapi.classify_error(exc)
                if kind == "rejected" or kind == "auth":
                    # Definitivo, no retry
                    return (None, 0.0, exc)
                # Transitorio: esperar y reintentar
                backoff = RETRY_BACKOFF_BASE ** attempt
                logger.warning(
                    "place attempt %d/%d fallo (%s), retry en %.1fs: %s",
                    attempt, MAX_RETRIES, kind, backoff, str(exc)[:120],
                )
                time.sleep(backoff)

        return (None, 0.0, last_error)

    # ---------------------------------------------------- poll fill
    def _poll_fill(self, order_id: str, expected_shares: float) -> float:
        """Consulta el fill status de una orden FAK hasta N veces.

        FAK matcha inmediato pero Polymarket puede tardar 500-1500ms en reportar.
        """
        for attempt in range(FILL_POLL_ATTEMPTS):
            try:
                st = self.client.get_order(order_id) or {}
                filled = float(
                    st.get("size_matched")
                    or st.get("filled_size")
                    or st.get("size_filled")
                    or 0.0
                )
                if filled > 0:
                    return filled
                # Si status=matched pero sin campo size -> asumir full fill
                if st.get("status") in ("matched", "filled"):
                    return expected_shares
            except Exception as exc:
                logger.debug("poll_fill attempt %d: %s", attempt, exc)

            if attempt < FILL_POLL_ATTEMPTS - 1:
                time.sleep(FILL_POLL_DELAY_MS / 1000.0)

        return 0.0

    # ---------------------------------------------------- mark helpers
    def _wait_gtc_fill_or_cancel(
        self,
        order_id: str,
        timeout_sec: float,
        market_label: str,
        pos_id: str,
    ) -> Tuple[float, Optional[str]]:
        """GTC_5MIN_V1: espera fill hasta timeout_sec polleando cada 2s.

        Si matchea → devuelve (shares_filled, None).
        Si expira → intenta cancel_order(order_id) y devuelve (0, reason).
        """
        start = time.time()
        poll_interval = 2.0
        last_filled = 0.0
        while time.time() - start < timeout_sec:
            time.sleep(poll_interval)
            try:
                status = self.client.get_order(order_id) or {}
                size_matched = float(status.get("size_matched") or 0.0)
                if size_matched > last_filled:
                    last_filled = size_matched
                # estado "MATCHED" final
                if str(status.get("status", "")).upper() in ("MATCHED", "FILLED"):
                    return last_filled, None
                # si polymarket ya la cancelo por otra razon, salimos
                if str(status.get("status", "")).upper() in ("CANCELED", "CANCELLED"):
                    return last_filled, "cancelled_by_server"
            except Exception as exc:  # polling no rompe el bot
                log_warning(
                    "gtc_poll_error",
                    module="copy_executor",
                    extra={"pos_id": pos_id, "order_id": order_id, "err": str(exc)[:120]},
                )
        # Timeout → intentar cancelar
        try:
            self.client.cancel_order(order_id)
        except Exception as exc:
            log_warning(
                "gtc_cancel_failed",
                module="copy_executor",
                extra={"pos_id": pos_id, "order_id": order_id, "err": str(exc)[:120]},
            )
        return last_filled, "timeout"

    def _mark_filled(self, pos_id, order_id, price, shares, filled_usdc,
                     market_label, side_str):
        """Position filleada: update + notify."""
        update_record("Position", pos_id, {
            "pending_fill": False,
            "order_id": str(order_id),
            "entry_price": price,
            "current_price": price,
            "size_usdc": filled_usdc,
            "size_tokens": round(shares, 4),
        })
        _alert_once(
            f"ok:{pos_id}",
            f"ÃÂ¢ÃÂÃÂ <b>COPY-TRADE LLENADO</b>\n{market_label}\n"
            f"{side_str} {shares:.2f} sh @ {price:.3f} (~${filled_usdc:.2f})\n"
            f"order_id: <code>{order_id}</code>",
        )
        log_decision(
            reason="copy_fill_ok",
            market=market_label,
            strategy="whale_consensus",
            size=filled_usdc,
            extra={"pos_id": pos_id, "order_id": str(order_id),
                   "shares": shares, "price": price},
        )

    def _mark_closed(self, pos_id, reason, market_label):
        """Cerrar Position con razon."""
        try:
            update_record("Position", pos_id, {
                "status": "closed",
                "pending_fill": False,
                "close_reason": str(reason)[:120],
                "close_time": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.warning("mark_closed pos=%s fallo: %s", pos_id, exc)
        log_warning(
            "copy_closed_unfilled",
            module="copy_executor",
            extra={"pos_id": pos_id, "reason": reason, "market": market_label},
        )

    def _mark_expired(self, pos_id, market_label, age_sec):
        """Position pending > 5min: auto-expirar."""
        self._mark_closed(pos_id, f"expired_{int(age_sec)}s", market_label)
        _alert_once(
            f"exp:{pos_id}",
            f"ÃÂ¢ÃÂÃÂ± <b>Copy-trade expirÃÂÃÂ³</b>\n{market_label}\n"
            f"Pending por {age_sec/60:.1f}min sin fillear ÃÂ¢ÃÂÃÂ precio probablemente cambiÃÂÃÂ³.",
        )
