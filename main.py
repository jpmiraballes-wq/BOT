import os, time, requests, json
from dotenv import load_dotenv
load_dotenv()

BASE44_API_KEY = os.getenv("BASE44_API_KEY")
BASE44_APP_ID = os.getenv("BASE44_APP_ID")
CAPITAL_USDC = float(os.getenv("CAPITAL_USDC", "95"))
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
MAX_DEPLOYED = 0.6

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

def place_order(client, market, size_usdc=5.0):
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        token_id = market["yes_token"]
        if not token_id:
            return None
        price = round(min(max(market["best_bid"] + 0.01, 0.02), 0.97), 2)
        size = max(round(size_usdc / price, 2), 15.0)
        signed_order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY))
        resp = client.post_order(signed_order, OrderType.GTC)
        log("INFO", f"Orden {size}@{price} | {market['market_title'][:50]}")
        return resp
    except Exception as e:
        log("ERROR", f"place_order: {e}")
        return None

def main():
    print("[INFO] Bot v2.7 arrancando...")
    client = get_clob_client()
    if not client:
        print("[ERROR] No se pudo conectar al CLOB")
        return
    print("[INFO] Conectado al CLOB OK")
    cycle = 0
    deployed = 0
    while True:
        cycle += 1
        print(f"\n=== Ciclo #{cycle} ===")
        opportunities = scan_markets()
        if opportunities and deployed < CAPITAL_USDC * MAX_DEPLOYED:
            best = sorted(opportunities, key=lambda x: x["spread_pct"], reverse=True)[0]
            print(f"[INFO] Mejor: {best['spread_pct']}% | {best['market_title'][:60]}")
            result = place_order(client, best, size_usdc=min(5.0, CAPITAL_USDC * 0.05))
            if result:
                deployed += 5.0
        update_system_state({
            "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_hours": round(cycle * 30 / 3600, 2),
            "capital_deployed": deployed,
            "mode": "live"
        })
        time.sleep(30)

if __name__ == "__main__":
    main()
