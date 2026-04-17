#!/usr/bin/env python3
"""
Deposita USDC a Polymarket via Bridge API - version corregida
"""
import os
import time
import requests
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
RPC = "https://polygon-mainnet.g.alchemy.com/v2/Jfo7UHHxiaq3qduY1XKhW"

w3 = Web3(Web3.HTTPProvider(RPC))

USDC = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
]

def main():
    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    usdc = w3.eth.contract(address=USDC, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)

    usdc_bal = usdc.functions.balanceOf(wallet).call()
    pusd_bal = pusd.functions.balanceOf(wallet).call()
    print(f"USDC: {usdc_bal/1e6:.4f} | pUSD: {pusd_bal/1e6:.4f}")

    if usdc_bal < 1_000_000:
        print("ERROR: no hay USDC suficiente")
        return

    # Pedir direccion al Bridge
    print("\nPidiendo direccion al Bridge API...")
    resp = requests.post(
        "https://bridge.polymarket.com/deposit",
        json={"address": wallet},
        timeout=15
    )
    data = resp.json()

    # Extraer solo la direccion EVM (string)
    deposit_addr = None
    if "address" in data and isinstance(data["address"], dict):
        deposit_addr = data["address"]["evm"]
    elif "evm" in data:
        deposit_addr = data["evm"]

    if not deposit_addr:
        print(f"Error obteniendo direccion: {data}")
        return

    print(f"Direccion EVM: {deposit_addr}")

    # Enviar USDC
    amount = int(usdc_bal * 0.95)
    print(f"Enviando {amount/1e6:.2f} USDC...")

    nonce = w3.eth.get_transaction_count(wallet)
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(deposit_addr),
        amount
    ).build_transaction({
        "from": wallet, "nonce": nonce, "gas": 100000,
        "gasPrice": w3.eth.gas_price, "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"TX: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    print(f"Status: {receipt['status']}")

    if receipt['status'] == 1:
        print("\nEsperando conversion a pUSD (hasta 3 min)...")
        for i in range(9):
            time.sleep(20)
            p = pusd.functions.balanceOf(wallet).call()
            u = usdc.functions.balanceOf(wallet).call()
            print(f"  [{i+1}] pUSD: {p/1e6:.4f} | USDC: {u/1e6:.4f}")
            if p > 1_000_000:
                print(f"\npUSD recibido! Ahora corre: python3 main.py")
                return
        print("\nBridge puede tardar mas. Espera 5 min y corre python3 main.py")
    else:
        print("ERROR: transaccion fallida")

if __name__ == "__main__":
    main()
