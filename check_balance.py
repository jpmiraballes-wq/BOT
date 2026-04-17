from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv
import os, requests
load_dotenv()

ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "Jfo7UHHxiaq3qduY1XKhW")
w3 = Web3(Web3.HTTPProvider(f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"))
wallet = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))

# 1. USDC nativo on-chain
USDC = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
abi = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
usdc = w3.eth.contract(address=USDC, abi=abi)
bal_usdc = usdc.functions.balanceOf(wallet).call()
print(f"USDC on-chain:     ${bal_usdc/1e6:.4f}")

# 2. pUSD (token colateral Polymarket)
PUSD = Web3.to_checksum_address("0x4Fabb145d64652a948d72533023f6E7A623C7C53")
try:
    pusd = w3.eth.contract(address=PUSD, abi=abi)
    bal_pusd = pusd.functions.balanceOf(wallet).call()
    print(f"pUSD on-chain:     ${bal_pusd/1e6:.4f}")
except Exception as e:
    print(f"pUSD error: {e}")

# 3. USDC.e (bridged)
USDCe = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
try:
    usdce = w3.eth.contract(address=USDCe, abi=abi)
    bal_usdce = usdce.functions.balanceOf(wallet).call()
    print(f"USDC.e on-chain:   ${bal_usdce/1e6:.4f}")
except Exception as e:
    print(f"USDC.e error: {e}")

# 4. CLOB balance via API
creds = ApiCreds(api_key=os.getenv("CLOB_API_KEY"), api_secret=os.getenv("CLOB_SECRET"), api_passphrase=os.getenv("CLOB_PASS"))
client = ClobClient(host="https://clob.polymarket.com", key=os.getenv("PRIVATE_KEY"), chain_id=POLYGON, creds=creds)
try:
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"CLOB balance:      {bal}")
except Exception as e:
    print(f"CLOB error: {e}")

# 5. Collateral token del CLOB
try:
    col_addr = client.get_collateral_address()
    print(f"Collateral token:  {col_addr}")
    col = w3.eth.contract(address=Web3.to_checksum_address(col_addr), abi=abi)
    bal_col = col.functions.balanceOf(wallet).call()
    print(f"Collateral bal:    ${bal_col/1e6:.4f}")
except Exception as e:
    print(f"Collateral error: {e}")
