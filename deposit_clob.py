#!/usr/bin/env python3
"""
Deposita USDC nativo desde la wallet al CLOB de Polymarket
"""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "")

# RPC
RPC_URL = f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}" if ALCHEMY_KEY else "https://polygon-rpc.com"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Contratos
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")  # USDC nativo Polygon
CLOB_DEPOSIT_ADDRESS = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")  # CTF Exchange

USDC_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

def main():
    print("💰 Depositando USDC en el CLOB de Polymarket...\n")
    
    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)
    
    # Balance
    balance_raw = usdc.functions.balanceOf(wallet).call()
    balance = balance_raw / 1e6
    print(f"💵 USDC en wallet: {balance:.2f} USDC")
    
    if balance < 1:
        print("❌ Balance insuficiente (menos de 1 USDC)")
        return
    
    # Usar el cliente CLOB para depositar
    print("\n🔗 Conectando al CLOB...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON
    )
    
    # Depositar via CLOB client
    amount_to_deposit = balance_raw  # Todo el USDC disponible
    amount_usdc = balance_raw / 1e6
    
    print(f"⏳ Depositando {amount_usdc:.2f} USDC en el CLOB...")
    
    try:
        result = client.deposit(amount_to_deposit)
        print(f"✅ Depósito exitoso: {result}")
        print(f"\n🚀 Ahora corre: python3 main.py")
    except Exception as e:
        print(f"❌ Error en depósito: {e}")
        print("\n💡 Intenta depositar manualmente desde:")
        print("   https://polymarket.com → conecta wallet → deposita USDC")

if __name__ == "__main__":
    main()
