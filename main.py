import os, time, requests
from dotenv import load_dotenv
load_dotenv()

BASE44_API_KEY = os.getenv("BASE44_API_KEY")
BASE44_APP_ID = os.getenv("BASE44_APP_ID")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASS = os.getenv("CLOB_PASS")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

BASE_URL = f"https://app.base44.com/api/apps/{BASE44_APP_ID}/entities"
HEADERS = {"Authorization": "Bearer " + (BASE44_API_KEY or ""), "Content-Type": "application/json"}
CLOB_URL = "https://clob.polymarket.com"

SYSTEM_STATE_ID = "69e37b6d619bec8376f68f53"
BOT_CONFIG_ID = "69e19150519da83ec0682e87"

stats = {"wins": 0, "losses": 0, "total_pnl": 0.0, "orders": 0}
start_time = time.time()

config = {
    "paused": False,
    "min_spread_pct": 2.0,
    "max_position_pct": 7.0,
    "max_open_orders": 5,
    "rebalance_interval_sec": 30,
    "max_order_usdc": 15.0,
}

def fetch_config():
    global config
    try:
        r = requests.get(f"{BASE_URL}/BotConfig/{BOT_CONFIG_ID}", headers=HEADERS, timeout=5)
        if r.status_code == 200:
            data = r.json()
            config["paused"] = bool(data.get("paused", False))
            config["min_spread_pct"] = float(data.get("min_spread_pct", 2.0))
            config["max_position_pct"] = float(data.get("max_position_pct", 7.0))
            config["max_open_orders"] = int(data.get("max_open_orders", 5)) if data.get("max_open_orders") else 5
            config["rebalance_interval_sec"] = int(data.get("rebalance_interval_sec", 30))
            capital = float(data.get("capital_usdc", 213))
            config["max_order_usdc"] = round(capital * config["max_position_pct"] / 100, 2)
            mode = data.get("mode", "live")
            print(f"[CONFIG] paused={config['paused']} | spread_min={config['min_spread_pct']}% | max_ord={config['max_open_orders']} | size_max=${config['max_order_usdc']} | mode={mode}")
        else:
            print(f"[WARN] fetch_config HTTP {r.status_code}")
    except Exception as e:
        print(f"[WARN] fetch_config: {e}")

def get_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASS)
    return ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=POLYGON, creds=creds)

def get_balance(client):
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(bal.get("balance", "0")) / 1e6
    except:
        return 0.0

def update_dashboard(balance, open_orders):
    uptime = round((time.time() - start_time) / 3600, 2)
    winrate = round(stats["wins"] / max(stats["wins"] + stats["losses"], 1) * 100, 1)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        r = requests.put(f"{BASE_URL}/SystemState/{SYSTEM_STATE_ID}", json={
            "mode": "live",
            "capital_total": round(balance, 4),
            "capital_deployed": round(open_orders * config["max_order_usdc"], 2),
            "daily_pnl": round(stats["total_pnl"], 4),
            "total_pnl": round(stats["total_pnl"], 4),
            "win_rate": winrate,
            "total_trades": stats["orders"],
            "open_positions": open_orders,
            "uptime_hours": uptime,
            "heartbeat_at": now,
            "last_heartbeat": now,
            "bot_version": "3.2"
        }, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            print(f"[INFO] Heartbeat OK")
        else:
            print(f"[WARN] Heartbeat HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"[WARN] update_dashboard: {e}")

def save_trade(market, price, size_usdc, order_id):
    try:
        r = requests.post(f"{BASE_URL}/Trade", json={
            "market": market[:100],
            "side": "BUY",
            "entry_price": price,
            "size_usdc": size_usdc,
            "strategy": "market_making",
            "status": "open",
            "entry_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "notes": f"order_id:{order_id}"
        }, headers=HEADERS, timeout=5)
        return r.json().get("id")
    except Exception as e:
        print(f"[WARN] save_trade: {e}")
        return None

def close_trade(trade_id, pnl, status):
    try:
        requests.put(f"{BASE_URL}/Trade/{trade_id}", json={
            "status": status,
            "pnl": round(pnl, 4),
            "exit_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }, headers=HEADERS, timeout=5)
    except:
        pass

def log_event(level, msg):
    print(f"[{level.upper()}] {msg}")
    try:
        requests.post(f"{BASE_URL}/LogEvent", json={
            "level": level, "message": msg[:200], "module": "main"
        }, headers=HEADERS, timeout=5)
    except:
        pass

def scan_markets():
    try:
        res = requests.get(
            "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&order=volume&ascending=false",
            timeout=10
        )
        markets = res.json()
        opps = []
        import json as jsonlib
        min_spread = config["min_spread_pct"] / 100.0
        for m in markets:
            try:
                spread = float(m.get("spread", 0) or 0)
                best_bid = float(m.get("bestBid", 0) or 0)
                volume = float(m.get("volume", 0) or 0)
                liquidity = float(m.get("liquidityClob", 0) or 0)
                if spread >= min_spread and volume >= 5000 and liquidity >= 1000 and best_bid > 0:
                    token_ids = jsonlib.loads(m.get("clobTokenIds", "[]") or "[]")
                    opps.append({
                        "title": m.get("question", "")[:80],
                        "yes_token": token_ids[0] if token_ids else "",
                        "best_bid": best_bid,
                        "spread_pct": round(spread * 100, 2),
                    })
            except:
                continue
        return opps
    except Exception as e:
        print(f"[ERROR] scan_markets: {e}")
        return []

def place_order(client, opp, balance):
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        if not opp["yes_token"]:
            return None, 0
        max_usdc = config["max_order_usdc"]
        size_usdc = min(balance * (config["max_position_pct"] / 100.0), max_usdc)
        price = round(min(max(opp["best_bid"] + 0.01, 0.02), 0.97), 2)
        size = max(round(size_usdc / price, 1), 5.0)
        signed = client.create_order(OrderArgs(token_id=opp["yes_token"], price=price, size=size, side=BUY))
        resp = client.post_order(signed, OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id", "")
        print(f"[INFO] Orden OK {size}@{price} (${size_usdc:.2f}) | {opp['title'][:50]}")
        return order_id, size_usdc
    except Exception as e:
        print(f"[ERROR] place_order: {e}")
        return None, 0

def check_pending(client, pending):
    still_open = []
    for item in pending:
        oid, tid, ep, sz, title = item
        try:
            order = client.get_order(oid)
            status = order.get("status", "")
            matched = float(order.get("size_matched", 0) or 0)
            if matched > 0 or status == "MATCHED":
                pnl = sz * 0.025
                stats["wins"] += 1
                stats["total_pnl"] += pnl
                stats["orders"] += 1
                log_event("WIN", f"GANASTE ${pnl:.3f} | {title[:50]}")
                close_trade(tid, pnl, "filled")
            elif status in ["CANCELLED", "EXPIRED"]:
                stats["losses"] += 1
                stats["orders"] += 1
                log_event("INFO", f"Orden cancelada/expirada | {title[:40]}")
                close_trade(tid, 0, "cancelled")
            else:
                still_open.append(item)
        except:
            still_open.append(item)
    return still_open

def main():
    print("[INFO] Bot v3.2 arrancando...")
    client = get_clob_client()
    print("[INFO] Conectado al CLOB OK")

    balance = get_balance(client)
    print(f"[INFO] Balance inicial: ${balance:.4f}")
    print(f"[INFO] SystemState ID: {SYSTEM_STATE_ID}")

    fetch_config()

    try:
        resp = client.cancel_all()
        cancelled = len(resp.get("canceled", []))
        if cancelled > 0:
            print(f"[INFO] {cancelled} ordenes viejas canceladas al arrancar")
    except:
        pass

    pending = []
    cycle = 0

    while True:
        cycle += 1
        print(f"\n=== Ciclo #{cycle} ===")

        fetch_config()

        if config["paused"]:
            print("[INFO] Bot PAUSADO desde el dashboard. Esperando...")
            update_dashboard(balance, len(pending))
            time.sleep(config["rebalance_interval_sec"])
            continue

        balance = get_balance(client)
        print(f"[INFO] Balance CLOB: ${balance:.4f}")

        if balance < 1.0:
            print("[WARN] Balance bajo. Esperando fondos...")
            update_dashboard(balance, len(pending))
            time.sleep(60)
            continue

        if pending:
            pending = check_pending(client, pending)
            print(f"[INFO] Ordenes abiertas: {len(pending)}")

        max_orders = config["max_open_orders"]
        if len(pending) < max_orders:
            opps = scan_markets()
            print(f"[INFO] {len(opps)} oportunidades encontradas")
            if opps:
                best = sorted(opps, key=lambda x: x["spread_pct"], reverse=True)[0]
                print(f"[INFO] Mejor: {best['spread_pct']}% | {best['title'][:60]}")
                oid, sz = place_order(client, best, balance)
                if oid:
                    tid = save_trade(best["title"], best["best_bid"], sz, oid)
                    pending.append((oid, tid, best["best_bid"], sz, best["title"]))
                    stats["orders"] += 1
        else:
            print(f"[INFO] Max ordenes alcanzado ({len(pending)}/{max_orders})")

        update_dashboard(balance, len(pending))
        time.sleep(config["rebalance_interval_sec"])

if __name__ == "__main__":
    main()
