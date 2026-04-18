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
                     entry_price, size_tokens, order_id):
        """Crea un registro Position(status=open) en Base44."""
        if not BASE44_API_KEY:
            return None
        payload = {
            "market": question or market_id,
            "side": "BUY",
            "entry_price": float(entry_price),
            "current_price": float(entry_price),
            "size_usdc": float(entry_price) * float(size_tokens),
            "pnl_unrealized": 0.0,
            "status": "open",
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
        """Las posiciones en Base44 no guardan token_id; lo resolvemos desde cache."""
        pid = position.get("id")
        cached = self._cache.get(pid)
        if cached:
            return cached.get("token_id"), cached.get("size_tokens")
        return None, None

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
            token_id, size_tokens = self._extract_token_id(pos)
            if not token_id or not size_tokens:
                # Posicion de una sesion anterior sin cache local; saltamos.
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
