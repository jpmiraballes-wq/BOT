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

# RPCs publicos de Polygon (sin API key)
RPC_URLS = [
    "https://rpc-mainnet.maticvigil.com",
    "https://rpc.ankr.com/polygon",
    "https://matic-mainnet.chainstacklabs.com",
    "https://polygon-bor-rpc.publicnode.com",
]

def get_w3():
    for rpc in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                print(f"✅ RPC conectado: {rpc}")
                return w3
        except Exception:
            continue
    raise Exception("No se pudo conectar a ningun RPC de Polygon")

w3 = get_w3()

# Contratos
USDC_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")

USDC_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
]

def main():
    print("Depositando USDC en el CLOB de Polymarket...\n")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    usdc = w3.eth.contract(address=USDC_ADDRESS, abi=USDC_ABI)

    balance_raw = usdc.functions.balanceOf(wallet).call()
    balance = balance_raw / 1e6
    print(f"USDC en wallet: {balance:.2f} USDC")

    if balance < 1:
        print("Balance insuficiente (menos de 1 USDC)")
        return

    print("\nConectando al CLOB...")
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=PRIVATE_KEY,
        chain_id=POLYGON
    )

    print(f"Depositando {balance:.2f} USDC en el CLOB...")

    try:
        result = client.deposit(balance_raw)
        print(f"Deposito exitoso: {result}")
        print("\nAhora corre: python3 main.py")
    except Exception as e:
        print(f"Error en deposito: {e}")
        print("\nDeposita manualmente en: https://polymarket.com")

if __name__ == "__main__":
    main()
