"""order_manager.py - Gestion de ordenes en Polymarket CLOB (v2.3).

Cambios v2.3:
  - Obtiene maker_fee_rate del mercado via client.get_market(token_id)
    antes de construir la orden, evitando el error
    "invalid fee rate (0), current market's maker fee: 1000".
  - Cache de fee_rate por token_id para no consultar en cada ciclo.
"""

import logging
import os
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from config import (
    CLOB_API_URL, POLYGON_CHAIN_ID, PRIVATE_KEY, WALLET_ADDRESS,
    MAX_CONCURRENT_MARKETS, ORDER_MAX_AGE_SECONDS, MIN_SPREAD_PCT,
)
try:
    from config import BUY_ONLY_MODE
except ImportError:
    BUY_ONLY_MODE = False

try:
    from config import MAX_ORDERS_PER_MARKET
except ImportError:
    MAX_ORDERS_PER_MARKET = 2

from decision_logger import log_decision, log_warning
from position_tracker import PositionTracker

logger = logging.getLogger(__name__)

SizeFn = Callable[[Dict[str, Any]], float]


class OrderManager:
    def __init__(self):
        self.client = None
        self.creds = None
        self._orders = {}
        self._fee_cache = {}  # token_id -> maker_fee_rate (int basis points)
        self.tracker = PositionTracker()

    def connect(self):
        logger.info("Inicializando ClobClient en %s (chain=%d)",
                    CLOB_API_URL, POLYGON_CHAIN_ID)
        self.client = ClobClient(
            CLOB_API_URL, key=PRIVATE_KEY, chain_id=POLYGON_CHAIN_ID,
            signature_type=0, funder=WALLET_ADDRESS,
        )
        self.creds = self.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
        logger.info("ClobClient listo. API key: %s... | BUY_ONLY_MODE=%s | MAX_ORDERS/MKT=%d",
                    self.creds.api_key[:8], BUY_ONLY_MODE, MAX_ORDERS_PER_MARKET)

    def create_or_derive_api_creds(self):
        env_key = os.getenv("CLOB_API_KEY", "").strip()
        env_secret = os.getenv("CLOB_SECRET", "").strip()
        env_pass = os.getenv("CLOB_PASS", "").strip()
        if env_key and env_secret and env_pass:
            logger.info("Usando credenciales CLOB del entorno (.env).")
            return ApiCreds(
                api_key=env_key, api_secret=env_secret, api_passphrase=env_pass,
            )
        try:
            creds = self.client.derive_api_key()
            logger.info("Credenciales API derivadas.")
            return creds
        except Exception as exc:
            logger.warning("No se pudieron derivar (%s). Creando nuevas.", exc)
            return self.client.create_api_key()

    # -------------------------------------------------------------- fee rate
    def get_maker_fee_rate(self, token_id):
        """Devuelve el maker_fee_rate del mercado (basis points, int).

        Polymarket exige que la orden firme con el mismo fee_rate que expone
        el mercado. Cacheamos para evitar consultar cada ciclo.
        """
        if token_id in self._fee_cache:
            return self._fee_cache[token_id]
        fee = 0
        try:
            market = self.client.get_market(token_id) or {}
            # El campo puede venir como 'maker_fee_rate_bps', 'makerFeeRate',
            # o anidado bajo 'fee' / 'fees'. Intentamos varias claves.
            candidates = [
                market.get("maker_fee_rate_bps"),
                market.get("makerFeeRate"),
                market.get("maker_fee_rate"),
                (market.get("fee") or {}).get("maker") if isinstance(market.get("fee"), dict) else None,
            ]
            for c in candidates:
                if c is not None:
                    fee = int(c)
                    break
        except Exception as exc:
            logger.warning("No se pudo obtener fee_rate para %s (%s); usando 0",
                           token_id[:10], exc)
            fee = 0
        self._fee_cache[token_id] = fee
        logger.info("Fee rate %s: %d bps", token_id[:10], fee)
        return fee

    # ----------------------------------------------------------------- utils
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

    def get_orders_per_market(self):
        counts = Counter()
        for order in self.get_open_orders():
            mid = order.get("market") or order.get("market_id")
            if mid:
                counts[mid] += 1
        return counts

    def get_active_market_ids(self):
        return list(self.get_orders_per_market().keys())

    # --------------------------------------------------------------- builder
    def _build_order_args(self, *, token_id, price, size, side):
        """Construye OrderArgs incluyendo fee_rate_bps si el SDK lo soporta."""
        fee = self.get_maker_fee_rate(token_id)
        try:
            return OrderArgs(
                token_id=token_id, price=price, size=size, side=side,
                fee_rate_bps=fee,
            )
        except TypeError:
            # SDK antiguo sin fee_rate_bps; degradamos sin fee.
            return OrderArgs(token_id=token_id, price=price, size=size, side=side)

    # ----------------------------------------------------------------- place
    def place_market_making_pair(self, opportunity, position_size_usdc):
        market_id = opportunity["market_id"]
        token_ids = opportunity.get("token_ids") or []
        if not token_ids:
            log_warning("opportunity_sin_token_ids", module="market_maker",
                        extra={"market": market_id})
            return []
        token_id = token_ids[0]
        mid = float(opportunity["mid"])
        half_spread = max(MIN_SPREAD_PCT / 2.0, 0.01)
        bid_price = self._round_price(mid - half_spread)
        ask_price = self._round_price(mid + half_spread)

        if ask_price - bid_price < 0.01:
            logger.info("Spread insuficiente en %s.", market_id)
            return []
        if position_size_usdc <= 0:
            logger.info("Size 0 en %s (Kelly/filtro).", market_id)
            return []

        if BUY_ONLY_MODE:
            size_per_side = self._round_size(position_size_usdc / mid)
        else:
            size_per_side = self._round_size((position_size_usdc / 2.0) / mid)

        if size_per_side < 5.0:
            logger.info("Tamano pequeno (%.2f) en %s.", size_per_side, market_id)
            return []

        edge = float(opportunity.get("spread_pct", 0.0)) / 2.0
        question = opportunity.get("question") or market_id
        log_decision(
            reason="place_pair" if not BUY_ONLY_MODE else "place_buy_only",
            market=question, strategy="market_maker",
            edge=edge, size=position_size_usdc,
            extra={"mid": mid, "bid": bid_price, "ask": ask_price,
                   "size_per_side": size_per_side, "buy_only": BUY_ONLY_MODE},
        )

        sides = ((BUY, bid_price),) if BUY_ONLY_MODE else ((BUY, bid_price), (SELL, ask_price))

        created = []
        for side, price in sides:
            try:
                args = self._build_order_args(
                    token_id=token_id, price=price, size=size_per_side, side=side,
                )
                signed = self.client.create_order(args)
                resp = self.client.post_order(signed, OrderType.GTC)
                order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
                if order_id:
                    self._orders[order_id] = {
                        "market_id": market_id, "token_id": token_id,
                        "side": side, "price": price, "size": size_per_side,
                        "ts": time.time(),
                    }
                    created.append(order_id)
                    logger.info("Orden %s %s @ %.3f x %.2f en %s -> %s",
                                side, token_id[:10], price, size_per_side,
                                market_id, order_id)
                    if side == BUY:
                        self.tracker.register_buy(
                            market_id=market_id, token_id=token_id,
                            question=question, entry_price=price,
                            size_tokens=size_per_side, order_id=order_id,
                        )
                else:
                    logger.warning("Respuesta sin orderID: %s", resp)
            except Exception as exc:
                logger.error("Error %s en %s: %s", side, market_id, exc)
        return created

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
            if age >= ORDER_MAX_AGE_SECONDS:
                try:
                    self.client.cancel(order_id=order_id)
                    cancelled += 1
                    self._orders.pop(order_id, None)
                    logger.info("Stale cancelada: %s (%.0fs)", order_id, age)
                except Exception as exc:
                    logger.error("No cancelada %s: %s", order_id, exc)
        if cancelled:
            logger.info("Canceladas %d stale", cancelled)
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

    def close_profitable_positions(self):
        try:
            return self.tracker.check_and_close(self.client)
        except Exception as exc:
            logger.error("close_profitable_positions fallo: %s", exc)
            return 0

    def refresh(self, opportunities, size_fn):
        self.cancel_stale_orders()
        orders_per_market = self.get_orders_per_market()
        active_markets = set(orders_per_market.keys())
        free_slots = MAX_CONCURRENT_MARKETS - len(active_markets)

        for opp in opportunities:
            market_id = opp["market_id"]
            if orders_per_market.get(market_id, 0) >= MAX_ORDERS_PER_MARKET:
                logger.info("Mercado %s ya tiene %d ordenes (max %d); saltando.",
                            market_id, orders_per_market[market_id],
                            MAX_ORDERS_PER_MARKET)
                continue
            if market_id not in active_markets and free_slots <= 0:
                continue
            size = size_fn(opp)
            if size <= 0:
                continue
            created = self.place_market_making_pair(opp, size)
            if created:
                if market_id not in active_markets:
                    active_markets.add(market_id)
                    free_slots -= 1
                orders_per_market[market_id] = (
                    orders_per_market.get(market_id, 0) + len(created)
                )
