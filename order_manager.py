import logging
import time
from datetime import datetime, timezone
from config import (
    PRIVATE_KEY, WALLET_ADDRESS, CLOB_HOST, CHAIN_ID,
    CAPITAL_USDC, MAX_POSITION_PCT, MAX_MARKETS
)

logger = logging.getLogger(__name__)

def get_clob_client():
    """Crea cliente CLOB de Polymarket."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID
        )
        return client
    except ImportError:
        logger.error("py-clob-client no instalado. Ejecuta: pip install py-clob-client")
        return None
    except Exception as e:
        logger.error(f"Error creando CLOB client: {e}")
        return None

def get_or_create_api_creds(client):
    """Obtiene o crea credenciales API de Polymarket."""
    try:
        creds = client.create_or_derive_api_creds()
        return creds
    except Exception as e:
        logger.error(f"Error con credenciales API: {e}")
        return None

def place_market_making_orders(market, state):
    """
    Coloca ordenes BUY/SELL alrededor del mid price.
    Retorna lista de order_ids colocados.
    """
    client = get_clob_client()
    if not client:
        return []

    yes_price = market["yes_price"]
    no_price = market["no_price"]
    mid = (yes_price + no_price) / 2
    spread = market["spread"]

    # Tamaño por orden
    size_usdc = min(CAPITAL_USDC * MAX_POSITION_PCT, 15.0)

    # Precios BUY y SELL alrededor del mid
    buy_price = round(mid - spread / 3, 3)
    sell_price = round(mid + spread / 3, 3)

    # Clamp precios entre 0.01 y 0.99
    buy_price = max(0.01, min(0.99, buy_price))
    sell_price = max(0.01, min(0.99, sell_price))

    orders_placed = []

    try:
        # Orden de compra YES
        buy_order = client.create_limit_order({
            "token_id": market["tokens"][0].get("token_id"),
            "price": buy_price,
            "side": "BUY",
            "size": size_usdc
        })
        if buy_order:
            orders_placed.append({"id": buy_order.get("orderID"), "side": "BUY", "price": buy_price, "time": time.time()})
            logger.info(f"✅ BUY ${size_usdc} @ {buy_price} — {market['title'][:40]}")

        # Orden de venta YES
        sell_order = client.create_limit_order({
            "token_id": market["tokens"][0].get("token_id"),
            "price": sell_price,
            "side": "SELL",
            "size": size_usdc
        })
        if sell_order:
            orders_placed.append({"id": sell_order.get("orderID"), "side": "SELL", "price": sell_price, "time": time.time()})
            logger.info(f"✅ SELL ${size_usdc} @ {sell_price} — {market['title'][:40]}")

    except Exception as e:
        logger.error(f"Error colocando ordenes en {market['title'][:40]}: {e}")

    return orders_placed

def cancel_stale_orders(open_orders, client=None):
    """Cancela ordenes sin fill de mas de 2 horas."""
    if not client:
        client = get_clob_client()
    if not client:
        return

    now = time.time()
    stale_ids = []

    for order in open_orders:
        age_hours = (now - order.get("time", now)) / 3600
        if age_hours > 2:
            stale_ids.append(order["id"])

    for order_id in stale_ids:
        try:
            client.cancel(order_id)
            logger.info(f"🗑️ Orden cancelada (stale): {order_id}")
        except Exception as e:
            logger.error(f"Error cancelando orden {order_id}: {e}")

    return stale_ids
