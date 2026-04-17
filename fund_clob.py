#!/usr/bin/env python3
"""
Aprueba y deposita USDC nativo en el CTF Exchange de Polymarket via Web3
"""
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

RPC = "https://polygon-mainnet.g.alchemy.com/v2/Jfo7UHHxiaq3qduY1XKhW"
w3 = Web3(Web3.HTTPProvider(RPC))

# Contratos
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
CTF_EXCHANGE = Web3.to_checksum_address("0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E")
NEG_RISK    = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

USDC_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

def main():
    if not w3.is_connected():
        print("ERROR: no se pudo conectar al RPC")
        return

    print(f"RPC conectado (block {w3.eth.block_number})")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)
    
    usdc = w3.eth.contract(address=USDC_NATIVE, abi=USDC_ABI)
    
    balance_raw = usdc.functions.balanceOf(wallet).call()
    balance = balance_raw / 1e6
    print(f"USDC nativo: {balance:.2f} USDC")

    if balance < 1:
        print("ERROR: balance insuficiente")
        return

    MAX_UINT = 2**256 - 1
    amount_to_approve = balance_raw  # aprueba todo el balance

    # Aprobar CTF Exchange
    allowance_ctf = usdc.functions.allowance(wallet, CTF_EXCHANGE).call()
    print(f"Allowance CTF Exchange actual: {allowance_ctf/1e6:.2f} USDC")

    if allowance_ctf < balance_raw:
        print("Aprobando CTF Exchange...")
        nonce = w3.eth.get_transaction_count(wallet)
        tx = usdc.functions.approve(CTF_EXCHANGE, MAX_UINT).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"CTF Exchange aprobado! TX: {tx_hash.hex()}")
    else:
        print("CTF Exchange ya tiene allowance suficiente")

    # Aprobar Neg Risk Adapter
    allowance_neg = usdc.functions.allowance(wallet, NEG_RISK).call()
    print(f"Allowance Neg Risk actual: {allowance_neg/1e6:.2f} USDC")

    if allowance_neg < balance_raw:
        print("Aprobando Neg Risk Adapter...")
        nonce = w3.eth.get_transaction_count(wallet)
        tx = usdc.functions.approve(NEG_RISK, MAX_UINT).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"Neg Risk aprobado! TX: {tx_hash.hex()}")
    else:
        print("Neg Risk ya tiene allowance suficiente")

    print("\nAllowances configuradas. Verificando balance en CLOB...")
    
    # Verificar balance CLOB via API
    import requests
    resp = requests.get(f"https://clob.polymarket.com/balance?address={wallet}")
    print(f"Balance CLOB: {resp.text}")

    print("\nAhora corre: python3 main.py")

if __name__ == "__main__":
    main()
