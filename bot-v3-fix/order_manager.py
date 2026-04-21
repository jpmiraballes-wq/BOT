"""order_manager.py - v3 AGGRESSIVE FILLS
Cambio critico vs v2: en vez de poner BUY al mid-half_spread (que no se llena
nunca porque esta dentro del bid-ask), ponemos BUY al BEST_ASK (cruza el
spread -> fill inmediato). Idem SELL al BEST_BID. Sacrificamos edge por
fills reales.
"""

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config import (
    CLOB_API_URL, POLYGON_CHAIN_ID, PRIVATE_KEY, WALLET_ADDRESS,
    MAX_CONCURRENT_MARKETS, ORDER_MAX_AGE_SECONDS, MIN_SPREAD_PCT,
)
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

SizeFn = Callable[[Dict[str, Any]], float]

# Edad agresiva: si en 10 min no hubo fill, cancelar y rotar.
STALE_ORDER_MAX_AGE_SECONDS = 10 * 60


class OrderManager:
    def __init__(self):
        self.client = None
        self.creds = None
        self._orders = {}

    def connect(self):
        logger.info("Inicializando ClobClient en %s (chain=%d)",
                    CLOB_API_URL, POLYGON_CHAIN_ID)
        self.client = ClobClient(
            CLOB_API_URL, key=PRIVATE_KEY, chain_id=POLYGON_CHAIN_ID,
            signature_type=0, funder=WALLET_ADDRESS,
        )
        self.creds = self.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
        logger.info("ClobClient listo. API key: %s...", self.creds.api_key[:8])

    def create_or_derive_api_creds(self):
        try:
            creds = self.client.derive_api_key()
            logger.info("Credenciales API derivadas.")
            return creds
        except Exception as exc:
            logger.warning("No se pudieron derivar (%s). Creando nuevas.", exc)
            return self.client.create_api_key()

    @staticmethod
    def _round_price(price):
        price = max(0.01, min(0.99, price))
        return round(price * 100) / 100.0

    @staticmethod
    def _round_size(size):
        return round(max(5.0, size), 2)

    def get_open_orders(self):
        try:
            return self.client.get_orders() or []
        except Exception as exc:
            logger.error("Error obteniendo ordenes: %s", exc)
            return []

    def get_active_market_ids(self):
        ids = set()
        for order in self.get_open_orders():
            mid = order.get("market") or order.get("market_id")
            if mid:
                ids.add(mid)
        return list(ids)

    def place_aggressive_buy(self, opportunity, position_size_usdc):
        """
        V3: En vez de market making con pair limit, hacemos BUY agresivo
        al best_ask. Se llena inmediato. Si la tesis es buena (edge > fees),
        ganamos.
        """
        market_id = opportunity["market_id"]
        token_ids = opportunity.get("token_ids") or []
        if not token_ids:
            log_warning("opportunity_sin_token_ids", module="market_maker",
                        extra={"market": market_id})
            return []
        token_id = token_ids[0]
        best_ask = float(opportunity.get("ask") or opportunity.get("mid"))
        mid = float(opportunity["mid"])

        if position_size_usdc <= 0:
            return []

        # Pagamos el ask (cruzamos spread) -> fill inmediato.
        price = self._round_price(best_ask)
        size = self._round_size(position_size_usdc / price)
        if size < 5.0:
            logger.info("Size pequeno (%.2f) en %s.", size, market_id)
            return []

        edge = float(opportunity.get("spread_pct", 0.0)) / 2.0
        log_decision(
            reason="aggressive_buy",
            market=opportunity.get("question") or market_id,
            strategy="market_maker", edge=edge, size=position_size_usdc,
            extra={"mid": mid, "ask": best_ask, "price_paid": price, "size": size},
        )

        try:
            args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            if order_id:
                self._orders[order_id] = {
                    "market_id": market_id, "token_id": token_id,
                    "side": BUY, "price": price, "size": size,
                    "ts": time.time(),
                }
                logger.info("AGG BUY %s @ %.3f x %.2f en %s -> %s",
                            token_id[:10], price, size, market_id, order_id)
                return [order_id]
            logger.warning("Respuesta sin orderID: %s", resp)
            return []
        except Exception as exc:
            logger.error("Error AGG BUY en %s: %s", market_id, exc)
            return []

    # Alias compat con main.py
    def place_market_making_pair(self, opportunity, position_size_usdc):
        return self.place_aggressive_buy(opportunity, position_size_usdc)

    def cancel_stale_orders(self):
        now = time.time()
        cancelled = 0
        for order in self.get_open_orders():
            order_id = order.get("id") or order.get("orderID")
            local = self._orders.get(order_id)
            if local:
                age = now - local["ts"]
            else:
                created_at = order.get("created_at") or order.get("createdAt") or 0
                try:
                    created_at = float(created_at)
                    if created_at > 10_000_000_000:
                        created_at /= 1000.0
                    age = now - created_at if created_at else 0
                except (TypeError, ValueError):
                    age = 0
            # V3: 10 min de timeout, no 2 horas.
            if age >= STALE_ORDER_MAX_AGE_SECONDS:
                try:
                    self.client.cancel(order_id=order_id)
                    cancelled += 1
                    self._orders.pop(order_id, None)
                    logger.info("Stale cancelada: %s (%.0fs)", order_id, age)
                except Exception as exc:
                    logger.error("No cancelada %s: %s", order_id, exc)
        if cancelled:
            logger.info("Canceladas %d stale (v3, 10min timeout)", cancelled)
        return cancelled

    def cancel_all(self):
        try:
            self.client.cancel_all()
            count = len(self._orders)
            self._orders.clear()
            logger.info("cancel_all (%d local)", count)
            return count
        except Exception as exc:
            logger.error("cancel_all fallo: %s", exc)
            return 0

    def refresh(self, opportunities, size_fn):
        self.cancel_stale_orders()
        active_markets = set(self.get_active_market_ids())
        free_slots = MAX_CONCURRENT_MARKETS - len(active_markets)
        if free_slots <= 0:
            logger.info("Slots llenos (%d/%d).", len(active_markets), MAX_CONCURRENT_MARKETS)
            return
        for opp in opportunities:
            if free_slots <= 0:
                break
            if opp["market_id"] in active_markets:
                continue
            size = size_fn(opp)
            if size <= 0:
                continue
            created = self.place_aggressive_buy(opp, size)
            if created:
                active_markets.add(opp["market_id"])
                free_slots -= 1
