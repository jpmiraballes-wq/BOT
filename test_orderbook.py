"""
Test: leer order book real de Polymarket CLOB
"""
import os, json, requests
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CLOB_URL = "https://clob.polymarket.com"

def get_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    return ClobClient(host=CLOB_URL, key=PRIVATE_KEY, chain_id=POLYGON)

def main():
    client = get_client()
    creds = client.derive_api_key()
    client.set_api_creds(creds)
    print("✅ Autenticado")

    # Obtener mercados activos desde CLOB
    print("\n📊 Buscando mercados con spread en order book...")
    
    # Primero obtener algunos token_ids desde gamma API
    res = requests.get("https://gamma-api.polymarket.com/markets?limit=20&active=true&closed=false&order=volume&ascending=false")
    markets = res.json()
    
    spreads_found = []
    
    for m in markets[:20]:
        try:
            tokens = m.get("tokens", [])
            question = m.get("question", "")[:60]
            
            # Si no hay tokens en gamma, buscar en CLOB directamente
            if not tokens:
                # Intentar con conditionId
                condition_id = m.get("conditionId", "")
                if not condition_id:
                    continue
            
            for token in tokens:
                token_id = token.get("token_id", "")
                if not token_id:
                    continue
                
                # Obtener order book
                book = client.get_order_book(token_id)
                
                if book and hasattr(book, 'bids') and hasattr(book, 'asks'):
                    bids = book.bids
                    asks = book.asks
                    
                    if bids and asks:
                        best_bid = float(bids[0].price) if bids else 0
                        best_ask = float(asks[0].price) if asks else 1
                        spread = best_ask - best_bid
                        
                        if spread > 0.01:
                            spreads_found.append({
                                "market": question,
                                "token_id": token_id,
                                "bid": best_bid,
                                "ask": best_ask,
                                "spread": spread
                            })
                            print(f"  🎯 Spread: {spread*100:.2f}% | Bid:{best_bid} Ask:{best_ask} | {question}")
        except Exception as e:
            pass
    
    if not spreads_found:
        print("No se encontraron spreads en order book de los primeros 20 mercados")
        
        # Intentar listar mercados directamente desde CLOB
        print("\n📋 Mercados disponibles en CLOB:")
        try:
            clob_markets = client.get_markets()
            print(f"Total mercados CLOB: {len(clob_markets) if clob_markets else 0}")
            if clob_markets:
                for cm in list(clob_markets)[:5]:
                    print(f"  - {cm}")
        except Exception as e:
            print(f"Error get_markets: {e}")

if __name__ == "__main__":
    main()
