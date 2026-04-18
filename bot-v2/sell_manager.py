"""sell_manager.py - Cierra posiciones abiertas al alcanzar TP o SL.

Lee las posiciones con status=open desde el PositionTracker, obtiene el
precio actual via CLOB orderbook, calcula pnl_pct y coloca una orden SELL
al precio de mercado cuando:
  pnl_pct >= PROFIT_TARGET_PCT  (default +15%)  ->  take profit
  pnl_pct <= STOP_LOSS_PCT      (default -30%)  ->  stop loss

Nota: esta es una implementacion auto-contenida que complementa la que
ya vive en PositionTracker.check_and_close(). Se puede invocar como:
    from sell_manager import SellManager
    SellManager(order_manager).scan_and_close()
"""

import logging
from typing import Any, Dict, List, Optional

import requests

from config import CLOB_API_URL

try:
    from config import PROFIT_TARGET_PCT
except ImportError:
    PROFIT_TARGET_PCT = 0.15

try:
    from config import STOP_LOSS_PCT
except ImportError:
    STOP_LOSS_PCT = -0.30

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import SELL

from decision_logger import log_decision, log_close

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


class SellManager:
    def __init__(self, order_manager):
        """order_manager debe exponer:
           .client (ClobClient autenticado)
           .tracker (PositionTracker con list_open / _mark_closed / _cache)
           .get_maker_fee_rate(token_id)
        """
        self.om = order_manager

    # -------------------------------------------------------- price lookup
    @staticmethod
    def _get_mid_price(token_id):
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
            logger.debug("Mid no disponible %s: %s", token_id[:10], exc)
            return None

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _round_price(price):
        price = max(0.01, min(0.99, price))
        return round(price * 100) / 100.0

    def _extract_token(self, pos):
        token_id = pos.get("token_id")
        size_tokens = pos.get("size_tokens")
        if token_id and size_tokens:
            try:
                return token_id, float(size_tokens)
            except (TypeError, ValueError):
                pass
        cached = self.om.tracker._cache.get(pos.get("id"))
        if cached:
            return cached.get("token_id"), cached.get("size_tokens")
        return None, None

    # --------------------------------------------------------------- main
    def scan_and_close(self):
        tracker = self.om.tracker
        client = self.om.client
        if client is None:
            return 0

        open_positions = tracker.list_open()
        if not open_positions:
            return 0

        closed = 0
        for pos in open_positions:
            pid = pos.get("id")
            token_id, size_tokens = self._extract_token(pos)
            if not token_id or not size_tokens:
                continue

            entry = float(pos.get("entry_price") or 0.0)
            if entry <= 0:
                continue

            current = self._get_mid_price(token_id)
            if current is None:
                continue

            pnl_pct = (current - entry) / entry
            pnl_abs = (current - entry) * size_tokens

            hit_tp = pnl_pct >= PROFIT_TARGET_PCT
            hit_sl = pnl_pct <= STOP_LOSS_PCT
            if not (hit_tp or hit_sl):
                continue

            reason = "profit_target" if hit_tp else "stop_loss"
            sell_price = self._round_price(current)
            question = pos.get("market") or token_id[:10]

            log_decision(
                reason=reason, market=question, strategy="sell_manager",
                edge=pnl_pct, size=sell_price * size_tokens,
                extra={"entry": entry, "current": current,
                       "pnl_pct": pnl_pct, "pnl_abs": pnl_abs},
            )

            try:
                args = self.om._build_order_args(
                    token_id=token_id, price=sell_price,
                    size=round(float(size_tokens), 2), side=SELL,
                )
                signed = client.create_order(args)
                resp = client.post_order(signed, OrderType.GTC)
                sell_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
                if sell_id:
                    logger.info("SELL %s %s @ %.3f x %.2f -> %s (%s)",
                                reason, token_id[:10], sell_price, size_tokens,
                                sell_id, "%.2f%%" % (pnl_pct * 100))
                    tracker._mark_closed(pid, sell_price, pnl_abs)
                    tracker._cache.pop(pid, None)
                    log_close(market=question, strategy="sell_manager",
                              pnl=pnl_abs, reason=reason)
                    closed += 1
                else:
                    logger.warning("SELL sin orderID (%s): %s", reason, resp)
            except Exception as exc:
                logger.error("Error cerrando %s (%s): %s", pid, reason, exc)

        if closed:
            logger.info("SellManager cerro %d posiciones", closed)
        return closed
