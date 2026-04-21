"""position_tracker.py - Seguimiento de posiciones abiertas y cierre condicional.

Responsabilidades:
  1) Registrar una Position en Base44 cuando se rellena una orden BUY.
  2) Consultar posiciones abiertas (status=open) de Base44 en cada ciclo.
  3) Obtener el precio actual (mid) de cada mercado.
  4) Calcular PnL no realizado y cerrar (SELL) si alcanza PROFIT_TARGET_PCT
     o cae por debajo de STOP_LOSS_PCT.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import (
    BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL,
    CLOB_API_URL,
)

try:
    from config import PROFIT_TARGET_PCT
except ImportError:
    PROFIT_TARGET_PCT = 0.15

try:
    from config import STOP_LOSS_PCT
except ImportError:
    STOP_LOSS_PCT = -0.30

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _b44_headers():
    return {"api_key": BASE44_API_KEY or "", "Content-Type": "application/json"}


def _b44_endpoint(entity, record_id=None):
    base = "%s/api/apps/%s/entities/%s" % (BASE44_BASE_URL, BASE44_APP_ID, entity)
    if record_id:
        return "%s/%s" % (base, record_id)
    return base


class PositionTracker:
    """Mantiene y cierra posiciones abiertas.

    El OrderManager llama a:
      - register_buy(...) tras colocar una orden BUY exitosa.
      - check_and_close(clob_client) en cada ciclo del loop.
    """

    def __init__(self):
        self._cache = {}  # token_id -> position dict (para lookup rapido)

    # ----------------------------------------------------------------- create
    def register_buy(self, *, market_id, token_id, question,
                     entry_price, size_tokens, order_id,
                     strategy=None):  # tracking-meta-v1
        """Crea un registro Position(status=open) en Base44."""
        if not BASE44_API_KEY:
            return None
        _now_ts = time.time()
        payload = {
            "market": question or market_id,
            "side": "BUY",
            "entry_price": float(entry_price),
            "current_price": float(entry_price),
            "size_usdc": float(entry_price) * float(size_tokens),
            "size_tokens": float(size_tokens),
            "token_id": token_id,
            "order_id": order_id,
            "pnl_unrealized": 0.0,
            "status": "open",
            "pending_fill": True,
            "strategy": strategy or "market_maker",
            "question": question or "",
            "opened_at": _now_iso(),
            "opened_at_ts": _now_ts,
        }
        try:
            resp = requests.post(_b44_endpoint("Position"), json=payload,
                                 headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("Base44 Position create %d: %s",
                             resp.status_code, resp.text[:200])
                return None
            data = resp.json() if resp.content else {}
            pid = data.get("id") if isinstance(data, dict) else None
            # Cache interna: incluimos token_id y market_id para el cierre.
            if pid:
                self._cache[pid] = {
                    "id": pid, "market_id": market_id, "token_id": token_id,
                    "entry_price": float(entry_price),
                    "size_tokens": float(size_tokens),
                    "question": question, "order_id": order_id,
                    "opened_at": time.time(),
                }
            logger.info("Posicion registrada: %s @ %.3f x %.2f tokens (id=%s)",
                        question or market_id, entry_price, size_tokens, pid)
            return pid
        except requests.RequestException as exc:
            logger.error("register_buy fallo: %s", exc)
            return None

    # ------------------------------------------------------------------- read
    def list_open(self):
        """Devuelve las posiciones con status=open desde Base44."""
        if not BASE44_API_KEY:
            return []
        try:
            params = {
                "status": "open",
                "sort": "-created_date",
                "limit": 100,
            }
            resp = requests.get(_b44_endpoint("Position"), params=params,
                                headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("Base44 Position list %d: %s",
                             resp.status_code, resp.text[:200])
                return []
            data = resp.json()
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return data if isinstance(data, list) else []
        except requests.RequestException as exc:
            logger.error("list_open fallo: %s", exc)
            return []

    # ----------------------------------------------------------------- update
    def _mark_closed(self, position_id, exit_price, pnl):
        payload = {
            "status": "closed",
            "current_price": float(exit_price),
            "pnl_unrealized": float(pnl),
        }
        try:
            resp = requests.put(_b44_endpoint("Position", position_id),
                                json=payload, headers=_b44_headers(),
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("Base44 Position update %d: %s",
                             resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.error("_mark_closed fallo: %s", exc)

    # ------------------------------------------------------------- price feed
    def _get_current_price(self, token_id):
        """Consulta el mid-price actual de un token via CLOB orderbook."""
        try:
            url = "%s/book" % CLOB_API_URL
            resp = requests.get(url, params={"token_id": token_id},
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                return None
            book = resp.json() or {}
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            if best_bid and best_ask:
                return (best_bid + best_ask) / 2.0
            return best_bid or best_ask
        except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
            logger.debug("Precio no disponible para %s: %s", token_id[:10], exc)
            return None

    # ----------------------------------------------------------------- close
    def _extract_token_id(self, position):
        """Obtiene token_id y size_tokens. Prioriza los campos persistidos en
        Base44 para que sobrevivan a un reinicio del bot; si faltan, cae al
        cache local de la sesion actual.
        """
        token_id = position.get("token_id")
        size_tokens = position.get("size_tokens")
        if token_id and size_tokens:
            try:
                return token_id, float(size_tokens)
            except (TypeError, ValueError):
                pass
        pid = position.get("id")
        cached = self._cache.get(pid)
        if cached:
            return cached.get("token_id"), cached.get("size_tokens")
        return None, None

    # ---------------------------------------------- wallet balance guard
    _WALLET = "0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb"
    _DATA_API = "https://data-api.polymarket.com"

    def _has_wallet_balance(self, token_id, size_tokens):
        """Consulta data-api y devuelve True si wallet tiene >= size_tokens."""
        try:
            url = "%s/positions" % self._DATA_API
            resp = requests.get(
                url,
                params={"user": self._WALLET, "sizeThreshold": "0.01"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code >= 400:
                return True  # en caso de error de red, no bloqueamos
            positions = resp.json() or []
            tid = str(token_id)
            for p in positions:
                asset = str(p.get("asset") or p.get("tokenId") or p.get("token_id") or "")
                if asset == tid:
                    return float(p.get("size") or 0) >= float(size_tokens) * 0.95
            return False  # no encontrado = 0 en wallet
        except (requests.RequestException, ValueError) as exc:
            logger.debug("balance check fallo: %s", exc)
            return True  # fail-open para no bloquear por red

    def _mark_no_balance(self, position_id):
        payload = {
            "status": "closed",
            "close_reason": "no_balance_on_chain",
            "close_time": _now_iso(),
            "pnl_realized": 0.0,
            "pnl_unrealized": 0.0,
        }
        try:
            resp = requests.put(
                _b44_endpoint("Position", position_id),
                json=payload, headers=_b44_headers(), timeout=REQUEST_TIMEOUT
            )
            if resp.status_code >= 400:
                logger.error("mark_no_balance %d: %s",
                             resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.error("mark_no_balance fallo: %s", exc)

    def _cancel_active_sells(self, clob_client, token_id):
        """Cancela las ordenes SELL activas del CLOB sobre este token.

        El MM deja SELL vivas que reservan balance. Si no las cancelamos,
        la SELL del take-profit/stop-loss rebota con "not enough balance".
        """
        try:
            orders = clob_client.get_orders() or []
        except Exception as exc:
            logger.warning("cancel_active_sells: get_orders fallo: %s", exc)
            return 0
        tid = str(token_id)
        cancelled = 0
        for o in orders:
            side = (o.get("side") or "").upper()
            oid = o.get("id") or o.get("orderID") or o.get("orderId")
            ot = str(o.get("asset_id") or o.get("token_id") or o.get("tokenId") or "")
            if side == "SELL" and ot == tid and oid:
                try:
                    clob_client.cancel(order_id=oid)
                    cancelled += 1
                    logger.info("Cancelada SELL MM %s (token %s) para liberar balance",
                                str(oid)[:12], tid[:10])
                except Exception as exc:
                    logger.warning("cancel SELL %s fallo: %s", str(oid)[:12], exc)
        if cancelled:
            # Dar tiempo al CLOB para liberar la reserva antes de la nueva SELL.
            time.sleep(0.4)
        return cancelled

    def check_and_close(self, clob_client):
        """Revisa posiciones abiertas y coloca SELL si alcanzan el target.

        Usa el clob_client del OrderManager (ya autenticado) para vender.
        """
        if clob_client is None:
            return 0

        open_positions = self.list_open()
        if not open_positions:
            return 0

        closed = 0
        for pos in open_positions:
            pid = pos.get("id")
            # PATCH: ignorar posiciones pending_fill (aun no confirmadas en wallet)
            if pos.get("pending_fill") is True:
                logger.debug("Skip pending_fill position %s", pid)
                continue
            token_id, size_tokens = self._extract_token_id(pos)
            if not token_id or not size_tokens:
                # Posicion de una sesion anterior sin cache local; saltamos.
                continue
            # PATCH: verificar balance on-chain antes de intentar SELL
            if not self._has_wallet_balance(token_id, size_tokens):
                logger.warning(
                    "Position %s sin balance on-chain para %s (req %.2f tokens). "
                    "Marco closed (no_balance_on_chain).",
                    pid, (token_id or "")[:10], float(size_tokens)
                )
                self._mark_no_balance(pid)
                continue

            entry = float(pos.get("entry_price") or 0.0)
            if entry <= 0:
                continue

            current = self._get_current_price(token_id)
            if current is None:
                continue

            pnl_pct = (current - entry) / entry
            size_usdc = entry * size_tokens
            pnl_abs = (current - entry) * size_tokens

            hit_target = pnl_pct >= PROFIT_TARGET_PCT
            hit_stop = pnl_pct <= STOP_LOSS_PCT

            if not (hit_target or hit_stop):
                continue

            reason = "profit_target" if hit_target else "stop_loss"
            logger.info("Cerrando posicion %s: entry=%.3f current=%.3f pnl=%+.2f%% (%s)",
                        pid, entry, current, pnl_pct * 100, reason)

            # Liberar balance cancelando las SELL del MM sobre este token
            # antes de mandar la SELL de auto-close.
            self._cancel_active_sells(clob_client, token_id)

            try:
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.order_builder.constants import SELL
                sell_price = round(max(0.01, min(0.99, current)) * 100) / 100.0
                args = OrderArgs(token_id=token_id, price=sell_price,
                                 size=round(size_tokens, 2), side=SELL)
                signed = clob_client.create_order(args)
                resp = clob_client.post_order(signed, OrderType.GTC)
                sell_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
                if sell_id:
                    logger.info("SELL enviada %s @ %.3f x %.2f -> %s",
                                token_id[:10], sell_price, size_tokens, sell_id)
                    self._mark_closed(pid, sell_price, pnl_abs)
                    self._cache.pop(pid, None)
                    closed += 1
                else:
                    logger.warning("SELL sin orderID: %s", resp)
            except Exception as exc:
                logger.error("Error cerrando posicion %s: %s", pid, exc)

        if closed:
            logger.info("Cerradas %d posiciones este ciclo", closed)
        return closed
