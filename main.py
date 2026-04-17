import os, time, requests, json
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

def get_clob_balance(client):
    """Obtiene balance real del CLOB (custodial)"""
    try:
        # Metodo 1: via client
        bal = client.get_balance()
        if bal and float(bal) > 0:
            return float(bal)
    except:
        pass
    try:
        # Metodo 2: via API directa con firma
        from py_clob_client.headers.builder import create_level_2_headers
        ts = str(int(time.time()))
        headers = create_level_2_headers(
            secret=CLOB_SECRET,
            credentials={"key": CLOB_API_KEY, "passphrase": CLOB_PASS},
            method="GET", request_path="/balance-allowance",
            body="", timestamp=ts
        )
        res = requests.get(f"{CLOB_URL}/balance-allowance", headers={
            **headers,
            "POLY-API-KEY": CLOB_API_KEY,
        }, timeout=10)
        data = res.json()
        bal = float(data.get("balance", 0))
        return bal
    except Exception as e:
        print(f"[WARN] get_clob_balance fallback: {e}")
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
            return None

        # Usar maximo 20% del balance disponible por orden, minimo $1
        size_usdc = max(1.0, min(balance_usdc * 0.20, 4.0))
        price = round(min(max(market["best_bid"] + 0.01, 0.02), 0.97), 2)
        size = round(size_usdc / price, 1)

        print(f"[INFO] Intentando orden: {size} shares @ {price} (~${size_usdc:.2f}) | balance: ${balance_usdc:.2f}")

        signed_order = client.create_order(OrderArgs(token_id=token_id, price=price, size=size, side=BUY))
        resp = client.post_order(signed_order, OrderType.GTC)
        log("INFO", f"Orden OK {size}@{price} | {market['market_title'][:50]}")
        return resp
    except Exception as e:
        log("ERROR", f"place_order: {e}")
        return None

def main():
    print("[INFO] Bot v2.8 arrancando...")
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

        # Verificar balance real del CLOB
        balance = get_clob_balance(client)
        print(f"[INFO] Balance CLOB: ${balance:.2f}")

        if balance < 1.0:
            print("[WARN] Balance insuficiente en CLOB. Esperando...")
            update_system_state({
                "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "mode": "waiting_funds",
                "capital_total": balance
            })
            time.sleep(60)
            continue

        opportunities = scan_markets()
        if opportunities:
            best = sorted(opportunities, key=lambda x: x["spread_pct"], reverse=True)[0]
            print(f"[INFO] Mejor: {best['spread_pct']}% | {best['market_title'][:60]}")
            result = place_order(client, best, balance_usdc=balance)
            if result:
                deployed += balance * 0.20

        update_system_state({
            "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uptime_hours": round(cycle * 30 / 3600, 2),
            "capital_deployed": deployed,
            "capital_total": balance,
            "mode": "live"
        })
        time.sleep(30)

if __name__ == "__main__":
    main()
