import os
from dotenv import load_dotenv

load_dotenv()

# Wallet
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "0x8Cb774a116586685Ca07aE5AcfE6f57677ac42c3")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# Base44 — QuantPredict Pro
BASE44_API_KEY = os.getenv("BASE44_API_KEY")
BASE44_APP_ID = "69e1e225a40599eb44ced81e"
BASE44_URL = f"https://base44.app/api/apps/{BASE44_APP_ID}/entities"

# Capital
CAPITAL_USDC = float(os.getenv("CAPITAL_USDC", "30"))
MAX_POSITION_PCT = 0.05
MIN_SPREAD_PCT = 0.02
MAX_DRAWDOWN_PCT = 0.15
RESERVE_PCT = 0.20
MAX_MARKETS = 3
MAX_EXPOSURE_USDC = 150.0
STOP_LOSS_PER_POS = 5.0
CANCEL_ORDER_HOURS = 2

# Polymarket
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

def validate_config():
    errors = []
    if not PRIVATE_KEY:
        errors.append("PRIVATE_KEY no configurada")
    if not BASE44_API_KEY:
        errors.append("BASE44_API_KEY no configurada")
    if CAPITAL_USDC < 10:
        errors.append("CAPITAL_USDC muy bajo (minimo 10)")
    if errors:
        raise ValueError(f"Config errors: {', '.join(errors)}")
    print(f"✅ Config OK — Capital: ${CAPITAL_USDC} USDC")
