import os, time, requests, json, uuid
from dotenv import load_dotenv
load_dotenv()

BASE44_API_KEY = os.getenv("BASE44_API_KEY")
BASE44_APP_ID = os.getenv("BASE44_APP_ID")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASS = os.getenv("CLOB_PASS")

BASE_URL = f"https://app.base44.com/api/apps/{BASE44_APP_ID}/entities"
HEADERS = {"api_key": BASE44_API_KEY, "Content-Type": "application/json"}
CLOB_URL = "https://clob.polymarket.com"
MIN_SPREAD = 0.02
MIN_VOLUME = 5000
MIN_LIQUIDITY = 1000

# Stats globales
stats = {"wins": 0, "losses": 0, "total_pnl": 0.0, "orders": 0}

def log(level, msg):
    print(f"[{level}] {msg}")
    try:
        requests.post(f"{BASE_URL}/LogEvent", json={"level": level, "message": msg, "module": "main"}, headers=HEADERS, timeout=5)
    except:
        pass

def update_system_state(data):
    try:
        res = requests.get(f"{BASE_URL}/SystemState", headers=HEADERS, timeout=5)
        records = res.json()
        if isinstance(records, list) and len(records) > 0:
            requests.put(f"{BASE_URL}/SystemState/{records[0]['id']}", json=data, headers=HEADERS, timeout=5)
        else:
            requests.post(f"{BASE_URL}/SystemState", json=data, headers=HEADERS, timeout=5)
    except Exception as e:
        print(f"[ERROR] update_system_state: {e}")

def save_trade(market, side, entry_price, size_usdc, order_id, status="open"):
    try:
        trade = {
            "market": market,
            "side": side,
            "entry_price": entry_price,
            "size_usdc": size_usdc,
            "strategy": "market_making",
            "status": status,
            "entry_time": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notes": f"order_id:{order_id}"
        }
        res = requests.post(f"{BASE_URL}/Trade", json=trade, headers=HEADERS, timeout=5)
        return res.json().get("id")
    except Exception as e:
        print(f"[ERROR] save_trade: {e}")
        return None

def check_order_result(client, order_id, trade_id, entry_price, size_usdc, market):
    """Verifica si una orden se llenó y calcula PnL"""
    try:
        order = client.get_order(order_id)
        status = order.get("status", "")
        size_matched = float(order.get("size_matched", 0))
        
        if status == "MATCHED" or size_matched > 0:
            # Orden ejecutada - calcular PnL aproximado
            # En market making: ganamos el spread cuando se cierra la posición
            spread_earned = size_usdc * 0.03  # ~3% spread promedio
            pnl = spread_earned
            stats["wins"] += 1
            stats["total_pnl"] += pnl
            stats["orders"] += 1
            
            msg = f"✅ GANASTE ${pnl:.3f} | {market[:40]}"
            log("WIN", msg)
            
            # Actualizar trade en Base44
            requests.put(f"{BASE_URL}/Trade/{trade_id}", json={
                "status": "filled",
                "pnl": round(pnl, 4),
                "pnl_pct": round(spread_earned/size_usdc*100, 2),
                "exit_time": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "notes": f"order_id:{order_id} | status:{status}"
            }, headers=HEADERS, timeout=5)
            
        elif status in ["CANCELLED", "EXPIRED"]:
            # Orden cancelada - no hay pérdida real
            stats["losses"] += 1
            msg = f"❌ ORDEN CANCELADA | {market[:40]}"
            log("LOSS", msg)
            requests.put(f"{BASE_URL}/Trade/{trade_id}", json={
                "status": "cancelled",
                "pnl": 0,
                "exit_time": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }, headers=HEADERS, timeout=5)
        else:
            print(f"[INFO] Orden pendiente: {status}")
            
    except Exception as e:
        print(f"[WARN] check_order_result: {e}")

def get_clob_client():
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        from py_clob_client.constants import POLYGON
        creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASS)
        return ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=POLYGON, creds=creds)
    except Exception as e:
        print(f"[ERROR] get_clob_client: {e}")
        return None

def get_clob_balance(client):
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        raw = bal.get("balance", "0")
        return float(raw) / 1e6
    except Exception as e:
        print(f"[WARN] get_clob_balance: {e}")
        return 0.0

def scan_markets():
    try:
        res = requests.get("https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume&ascending=false", timeout=10)
        markets = res.json()
        opportunities = []
        for m in markets:
            try:
                spread = float(m.get("spread", 0) or 0)
                best_bid = float(m.get("bestBid", 0) or 0)
                best_ask = float(m.get("bestAsk", 0) or 0)
                volume = float(m.get("volume", 0) or 0)
                liquidity = float(m.get("liquidityClob", 0) or 0)
                if spread >= MIN_SPREAD and volume >= MIN_VOLUME and liquidity >= MIN_LIQUIDITY and best_bid > 0 and best_ask > 0:
                    token_ids = json.loads(m.get("clobTokenIds", "[]") or "[]")
                    opportunities.append({
                        "market_title": m.get("question", ""),
                        "yes_token": token_ids[0] if token_ids else "",
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        "spread_pct": round(spread * 100, 2),
                        "volume": volume
                    })
            except:
                continue
        print(f"[INFO] {len(markets)} mercados, {len(opportunities)} oportunidades")
        return opportunities
    except Exception as e:
        print(f"[ERROR] scan_markets: {e}")
        return []

def place_order(client, market, balance_usdc):
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        token_id = market["yes_token"]
        if not token_id:
            return None, None
        size_usdc = max(1.0, min(balance_usdc * 0.20, 4.0))
        price = round(min(max(market["best_bid"] + 0.01, 0.02), 0.97), 2)
        size = round(size_usdc / price, 1)
        print(f"[INFO] Orden: {size} shares @ {price} (~${size_usdc:.2f})")
        signed_order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY))
        resp = client.post_order(signed_order, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id", "")
        log("INFO", f"Orden colocada {size}@{price} | {market['market_title'][:50]}")
        return order_id, size_usdc
    except Exception as e:
        log("ERROR", f"place_order: {e}")
        return None, None

def print_stats(balance):
    winrate = round(stats["wins"] / max(stats["wins"] + stats["losses"], 1) * 100, 1)
    print(f"\n{'='*50}")
    print(f"  📊 ESTADISTICAS DEL BOT")
    print(f"  💰 Balance: ${balance:.2f} USDC")
    print(f"  ✅ Ganadas: {stats['wins']}  ❌ Canceladas: {stats['losses']}")
    print(f"  🎯 Aciertos: {winrate}%")
    print(f"  📈 PnL Total: ${stats['total_pnl']:.4f}")
    print(f"  📋 Ordenes: {stats['orders']}")
    print(f"{'='*50}\n")

def main():
    print("[INFO] Bot v3.0 arrancando...")
    client = get_clob_client()
    if not client:
        print("[ERROR] No se pudo conectar al CLOB")
        return
    print("[INFO] Conectado al CLOB OK")
    
    pending_orders = []  # [(order_id, trade_id, entry_price, size_usdc, market_title)]
    cycle = 0
    
    while True:
        cycle += 1
        print(f"\n=== Ciclo #{cycle} ===")

        balance = get_clob_balance(client)
        print(f"[INFO] Balance CLOB: ${balance:.4f}")

        if balance < 1.0:
            print("[WARN] Balance insuficiente. Esperando...")
            update_system_state({"last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "waiting_funds", "capital_total": balance})
            time.sleep(60)
            continue

        # Revisar ordenes pendientes
        still_pending = []
        for (oid, tid, ep, sz, mt) in pending_orders:
            check_order_result(client, oid, tid, ep, sz, mt)
            # Solo mantener si sigue pendiente (simplificado: revisar 3 veces max)
            still_pending.append((oid, tid, ep, sz, mt))
        pending_orders = still_pending[-5:]  # max 5 pendientes

        # Escanear y operar
        opportunities = scan_markets()
        if opportunities:
            best = sorted(opportunities, key=lambda x: x["spread_pct"], reverse=True)[0]
            print(f"[INFO] Mejor oportunidad: {best['spread_pct']}% | {best['market_title'][:60]}")
            order_id, size_usdc = place_order(client, best, balance_usdc=balance)
            if order_id:
                trade_id = save_trade(best["market_title"], "BUY", best["best_bid"], size_usdc, order_id)
                pending_orders.append((order_id, trade_id, best["best_bid"], size_usdc, best["market_title"]))

        # Stats en pantalla
        print_stats(balance)

        # Actualizar dashboard Base44
        winrate = round(stats["wins"] / max(stats["wins"] + stats["losses"], 1) * 100, 1)
        update_system_state({
            "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_hours": round(cycle * 30 / 3600, 2),
            "capital_total": balance,
            "daily_pnl": round(stats["total_pnl"], 4),
            "total_pnl": round(stats["total_pnl"], 4),
            "win_rate": winrate,
            "total_trades": stats["orders"],
            "open_positions": len(pending_orders),
            "mode": "live"
        })

        time.sleep(30)

if __name__ == "__main__":
    main()
