"""
Aprueba el CTF Exchange y Neg Risk Adapter para usar USDC del CLOB
"""
import os
from dotenv import load_dotenv
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

key = os.getenv("PRIVATE_KEY")
wallet = os.getenv("WALLET_ADDRESS")

print(f"🔑 Wallet: {wallet}")
print(f"⏳ Conectando al CLOB...")

client = ClobClient(HOST, key=key, chain_id=CHAIN_ID)

print(f"⏳ Aprobando contratos...")
try:
    result = client.set_allowances()
    print(f"✅ Allowances aprobadas: {result}")
except Exception as e:
    print(f"❌ Error: {e}")

print(f"\n🚀 Listo - ahora corre: python3 main.py")
