"""
Verifica balance on-chain y en CLOB de Polymarket
"""
import os
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
RPC = "https://polygon-mainnet.g.alchemy.com/v2/Jfo7UHHxiaq3qduY1XKhW"

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]

w3 = Web3(Web3.HTTPProvider(RPC))

print(f"✅ Conectado: bloque {w3.eth.block_number}")
print(f"📍 Wallet: {WALLET_ADDRESS}\n")

# Balance MATIC
matic = w3.eth.get_balance(WALLET_ADDRESS)
print(f"⛽ MATIC: {w3.from_wei(matic, 'ether'):.4f}")

# Balance USDC on-chain
usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=USDC_ABI)
balance = usdc.functions.balanceOf(Web3.to_checksum_address(WALLET_ADDRESS)).call()
print(f"💰 USDC on-chain: {balance / 1e6:.2f}")

# Balance CLOB
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=POLYGON,
        key=PRIVATE_KEY,
        signature_type=2,
        funder=WALLET_ADDRESS
    )
    clob_balance = client.get_balance()
    print(f"📊 USDC en CLOB: {clob_balance}")
except Exception as e:
    print(f"❌ CLOB error: {e}")
