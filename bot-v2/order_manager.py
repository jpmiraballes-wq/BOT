"""order_manager.py - Gestion de ordenes Polymarket CLOB (v2.4).

Cambios v2.4 (fix duplicados):
  - Antes de place_market_making_pair(), consulta PositionTracker.list_open()
    y salta si ya hay una posicion abierta con el mismo token_id.
  - refresh() construye un set active_tokens (ordenes + posiciones) para
    dedup global. MAX_ORDERS_PER_MARKET se mantiene como segundo cinturon.
"""

import logging
import os
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Set

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds, OrderArgs, OrderType,
    BalanceAllowanceParams, AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from config import (
    CLOB_API_URL, POLYGON_CHAIN_ID, PRIVATE_KEY, WALLET_ADDRESS,
    MAX_CONCURRENT_MARKETS, ORDER_MAX_AGE_SECONDS, MIN_SPREAD_PCT,
    POLYMARKET_FUNDER, POLYMARKET_SIGNATURE_TYPE,
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
        self._fee_cache = {}
        self.tracker = PositionTracker()

    def connect(self):
        logger.info("Inicializando ClobClient en %s (chain=%d, sig_type=%d, funder=%s)",
                    CLOB_API_URL, POLYGON_CHAIN_ID,
                    POLYMARKET_SIGNATURE_TYPE, POLYMARKET_FUNDER)
        self.client = ClobClient(
            CLOB_API_URL, key=PRIVATE_KEY, chain_id=POLYGON_CHAIN_ID,
            signature_type=POLYMARKET_SIGNATURE_TYPE, funder=POLYMARKET_FUNDER,
        )
        self.creds = self.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
        logger.info("ClobClient listo v2.4 dedup. API key: %s... | BUY_ONLY=%s | MAX_ORD/MKT=%d",
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
        if token_id in self._fee_cache:
            return self._fee_cache[token_id]
        fee = 0
        try:
            market = self.client.get_market(token_id) or {}
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

    def get_active_token_ids(self):
        """Tokens con orden BUY abierta en el CLOB ahora mismo."""
        tokens = set()
        for order in self.get_open_orders():
            side = (order.get("side") or "").upper()
            tid = order.get("asset_id") or order.get("token_id") or order.get("tokenId")
            if tid and side == "BUY":
                tokens.add(str(tid))
        return tokens

    def get_open_position_tokens(self):
        """Tokens con posicion Position(status=open) en Base44."""
        tokens = set()
        try:
            for pos in self.tracker.list_open() or []:
                tid = pos.get("token_id")
                if tid:
                    tokens.add(str(tid))
                pid = pos.get("id")
                cached = self.tracker._cache.get(pid) if pid else None
                if cached and cached.get("token_id"):
                    tokens.add(str(cached["token_id"]))
        except Exception as exc:
            logger.warning("No se pudieron listar posiciones abiertas: %s", exc)
        return tokens

    # --------------------------------------------------------------- builder
    def _build_order_args(self, *, token_id, price, size, side):
        fee = self.get_maker_fee_rate(token_id)
        try:
            return OrderArgs(
                token_id=token_id, price=price, size=size, side=side,
                fee_rate_bps=fee,
            )
        except TypeError:
            return OrderArgs(token_id=token_id, price=price, size=size, side=side)

    # ----------------------------------------------------------------- place
    def place_market_making_pair(self, opportunity, position_size_usdc,
                                 blocked_tokens: Optional[Set[str]] = None):
        market_id = opportunity["market_id"]
        token_ids = opportunity.get("token_ids") or []
        if not token_ids:
            log_warning("opportunity_sin_token_ids", module="market_maker",
                        extra={"market": market_id})
            return []
        token_id = str(token_ids[0])

        # DEDUP: si ya hay posicion abierta u orden BUY viva en este token, saltar.
        if blocked_tokens and token_id in blocked_tokens:
            log_decision(
                reason="skip_duplicate_token",
                market=opportunity.get("question") or market_id,
                strategy="market_maker",
                extra={"token_id": token_id[:10]},
            )
            logger.info("SKIP %s: ya hay posicion/orden abierta en token %s",
                        market_id, token_id[:10])
            return []

        mid = float(opportunity["mid"])
        # Bids/asks agresivos: saltar al frente del book (best_bid + 1 tick)
        # en vez de calcular desde mid - half_spread (que cae lejos y no se llena).
        TICK = 0.01
        # market_scanner pone "bid"/"ask" en el opportunity (best_bid/best_ask del orderbook).
        best_bid = float(opportunity.get("bid") or opportunity.get("best_bid") or 0.0)
        best_ask = float(opportunity.get("ask") or opportunity.get("best_ask") or 0.0)
        if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
            # Aggressive mode: 1 tick delante del best_bid / best_ask.
            # Cap inferior en mid - 0.01 y superior en mid + 0.01 para no
            # tocar el otro lado ni auto-fillearnos.
            # Subir el bid hasta el mid (no mid-TICK, porque si no se redondea
            # al best_bid y no sirve de nada). Con mid=0.155 y best_bid=0.14:
            # best_bid+TICK=0.15 < mid=0.155 -> bid final=0.15 (un tick mejor que best_bid)
            bid_price = self._round_price(min(best_bid + TICK, mid))
            ask_price = self._round_price(max(best_ask - TICK, mid))
            # Si cruzarian, abrir medio tick a cada lado del mid.
            if ask_price - bid_price < 0.01:
                bid_price = self._round_price(mid)
                ask_price = self._round_price(mid + TICK)
        else:
            # Fallback: comportamiento viejo si el opportunity no trae best_bid/best_ask.
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

        # Polymarket minimo $1 de notional por orden. Con mid bajo (ej 0.06)
        # y size=5 tokens, notional=$0.30 y la orden es rechazada.
        notional = size_per_side * mid
        if notional < 1.05:
            logger.info("Notional bajo ($%.2f < $1.05) en %s, skip.", notional, market_id)
            return []

        edge = float(opportunity.get("spread_pct", 0.0)) / 2.0
        question = opportunity.get("question") or market_id
        log_decision(
            reason="place_pair" if not BUY_ONLY_MODE else "place_buy_only",
            market=question, strategy="market_maker",
            edge=edge, size=position_size_usdc,
            extra={"mid": mid, "bid": bid_price, "ask": ask_price,
                   "size_per_side": size_per_side, "buy_only": BUY_ONLY_MODE,
                   "token_id": token_id[:10]},
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
                        self.tracker.register_buy(  # tracking-meta-v1
                            market_id=market_id, token_id=token_id,
                            question=question, entry_price=price,
                            size_tokens=size_per_side, order_id=order_id,
                            strategy="market_maker",
                        )
                else:
                    logger.warning("Respuesta sin orderID: %s", resp)
            except Exception as exc:
                exc_str = str(exc)
                if "not enough balance" in exc_str and side == SELL:
                    # Esperado en MM: no tenemos tokens hasta que el BUY se llene.
                    # El SELL se colocara despues via close_profitable_positions.
                    logger.info("SELL skip %s en %s: sin tokens aun (BUY pendiente fill)",
                                token_id[:10], market_id)
                else:
                    logger.error("Error %s en %s: %s", side, market_id, exc)

        # Marcar token como ocupado para el resto de este refresh().
        if created and blocked_tokens is not None:
            blocked_tokens.add(token_id)
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

        # DEDUP GLOBAL: tokens con orden BUY viva + tokens con posicion abierta.
        blocked_tokens: Set[str] = set()
        blocked_tokens.update(self.get_active_token_ids())
        blocked_tokens.update(self.get_open_position_tokens())
        if blocked_tokens:
            logger.info("Dedup activo: %d token_ids bloqueados", len(blocked_tokens))

        for opp in opportunities:
            market_id = opp["market_id"]
            token_id = str((opp.get("token_ids") or [""])[0])

            if token_id and token_id in blocked_tokens:
                logger.info("SKIP %s: token %s ya tiene posicion/orden abierta",
                            market_id, token_id[:10])
                continue

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

            created = self.place_market_making_pair(opp, size, blocked_tokens=blocked_tokens)
            if created:
                if market_id not in active_markets:
                    active_markets.add(market_id)
                    free_slots -= 1
                orders_per_market[market_id] = (
                    orders_per_market.get(market_id, 0) + len(created)
                )

    # ------------------------------------------------------------ news_trading
    def place_limit_buy(self, token_id: str, price: float, shares: float,
                        strategy: str = "news_trading") -> Optional[str]:
        """BUY limite simple para estrategias distintas de MM.

        Devuelve order_id o None. No registra en PositionTracker (lo hace la
        propia estrategia si le interesa). Lo registra en self._orders para
        tracking local y cancel_stale_orders.
        """
        assert self.client is not None
        price = self._round_price(price)
        size = self._round_size(shares)
        if size < 5.0:
            logger.info("place_limit_buy: size %.2f < 5, skip", size)
            return None
        try:
            args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
            signed = self.client.create_order(args)
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            if not order_id:
                logger.warning("place_limit_buy sin orderID: %s", resp)
                return None
            self._orders[order_id] = {
                "market_id": None,
                "token_id": token_id,
                "side": BUY,
                "price": price,
                "size": size,
                "ts": time.time(),
                "strategy": strategy,
            }
            log_decision(
                reason="limit_buy",
                market=token_id[:10],
                strategy=strategy,
                edge=0.0,
                size=size * price,
                extra={"price": price, "shares": size},
            )
            return order_id
        except Exception as exc:
            logger.error("place_limit_buy fallo token=%s: %s", token_id[:10], exc)
            return None

    def close_position_market(self, token_id: str, shares: float,
                              strategy: str = "news_trading") -> Optional[Dict[str, Any]]:
        """Cierra una posicion vendiendo al mejor bid (market-like).

        Pide el orderbook, encuentra best bid y coloca SELL limite a ese precio.
        Devuelve {"order_id": str, "avg_price": float} o None si falla.
        """
        assert self.client is not None
        size = self._round_size(shares)
        if size < 5.0:
            logger.info("close_position_market: shares %.2f < 5, skip", size)
            return None

        # Pre-check: balance CONDITIONAL real on-chain. Si el BUY original
        # nunca filleo (limit LIVE con filled=0), el proxy no tiene tokens
        # que vender y la SELL fallaria con "balance: 0, order amount: X".
        try:
            bal_resp = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                )
            )
            raw_bal = (bal_resp or {}).get("balance", "0")
            # El balance viene en unidades de 6 decimales (USDC-like).
            available = float(raw_bal) / 1_000_000.0
            if available < size:
                logger.warning(
                    "close SKIP: balance=%.4f < size=%.4f token=%s "
                    "(probable BUY original con filled=0)",
                    available, size, token_id[:10],
                )
                return None
        except Exception as _exc:
            logger.warning("close: get_balance_allowance fallo token=%s: %s",
                           token_id[:10], _exc)
            return None

        try:
            # Best bid via orderbook publico (no requiere auth)
            import requests as _rq
            resp = _rq.get("https://clob.polymarket.com/book",
                          params={"token_id": token_id}, timeout=10)
            if resp.status_code != 200:
                logger.warning("close: book HTTP %d", resp.status_code)
                return None
            book = resp.json()
            bids = book.get("bids") or []
            parsed = [(float(b.get("price", 0)), float(b.get("size", 0)))
                      for b in bids]
            parsed = [p for p in parsed if p[0] > 0]
            if not parsed:
                logger.warning("close: sin bids token=%s", token_id[:10])
                return None
            parsed.sort(key=lambda x: x[0], reverse=True)
            best_bid = parsed[0][0]
            price = self._round_price(best_bid)

            args = OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
            signed = self.client.create_order(args)
            # FOK: fill-or-kill. Si no hay liquidez suficiente en el book,
            # se cancela en lugar de quedar como limit huerfano.
            post = self.client.post_order(signed, OrderType.FOK)
            order_id = (post or {}).get("orderID") or (post or {}).get("orderId")
            if not order_id:
                logger.warning("close_position_market sin orderID: %s", post)
                return None
            log_decision(
                reason="close_market",
                market=token_id[:10],
                strategy=strategy,
                edge=0.0,
                size=size * price,
                extra={"price": price, "shares": size},
            )
            return {"order_id": order_id, "avg_price": price}
        except Exception as exc:
            logger.error("close_position_market fallo token=%s: %s", token_id[:10], exc)
            return None
