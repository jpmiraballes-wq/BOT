#!/usr/bin/env python3
"""
Convierte USDC nativo a pUSD via Bridge API de Polymarket
y aprueba los contratos necesarios para operar en el CLOB
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

# Contratos oficiales Polymarket (docs.polymarket.com/resources/contracts)
USDC_NATIVE  = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
PUSD         = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CTF          = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
CTF_EXCHANGE = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EX  = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")
NEG_RISK_AD  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
ONRAMP       = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

# ABI del CollateralOnramp para depositar USDC -> pUSD
ONRAMP_ABI = [
    {"inputs":[{"name":"amount","type":"uint256"},{"name":"receiver","type":"address"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},
]

MAX_UINT = 2**256 - 1

def approve_token(token_addr, spender_addr, token_name, spender_name, wallet, account):
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    allowance = token.functions.allowance(wallet, spender_addr).call()
    balance = token.functions.balanceOf(wallet).call()
    print(f"  {token_name} -> {spender_name}: allowance={allowance/1e6:.2f}, balance={balance/1e6:.2f}")
    if allowance < balance and balance > 0:
        print(f"  Aprobando {token_name} para {spender_name}...")
        nonce = w3.eth.get_transaction_count(wallet)
        tx = token.functions.approve(spender_addr, MAX_UINT).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 100000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"  OK! TX: {tx_hash.hex()[:20]}...")
        return True
    else:
        print(f"  Ya aprobado o sin balance.")
        return False

def main():
    if not w3.is_connected():
        print("ERROR: no se pudo conectar al RPC")
        return

    print(f"RPC conectado (block {w3.eth.block_number})\n")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    # 1. Verificar balances
    usdc_contract = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    pusd_contract = w3.eth.contract(address=PUSD, abi=ERC20_ABI)

    usdc_bal = usdc_contract.functions.balanceOf(wallet).call()
    pusd_bal = pusd_contract.functions.balanceOf(wallet).call()
    pol_bal = w3.eth.get_balance(wallet)

    print(f"Balances actuales:")
    print(f"  USDC nativo: {usdc_bal/1e6:.4f}")
    print(f"  pUSD:        {pusd_bal/1e6:.4f}")
    print(f"  POL (gas):   {pol_bal/1e18:.4f}\n")

    # 2. Si ya tenemos pUSD, saltar el depósito
    if pusd_bal > 1_000_000:  # mas de 1 pUSD
        print(f"Ya tienes {pusd_bal/1e6:.2f} pUSD. Saltando deposito.\n")
    elif usdc_bal > 1_000_000:
        # 3. Aprobar USDC para el Onramp
        print("Paso 1: Aprobar USDC para CollateralOnramp...")
        approve_token(USDC_NATIVE, ONRAMP, "USDC", "Onramp", wallet, account)
        time.sleep(3)

        # 4. Depositar USDC -> pUSD via Onramp
        amount = int(usdc_bal * 0.95)  # 95% dejando gas
        print(f"\nPaso 2: Depositando {amount/1e6:.2f} USDC -> pUSD via Onramp...")
        onramp = w3.eth.contract(address=ONRAMP, abi=ONRAMP_ABI)
        nonce = w3.eth.get_transaction_count(wallet)
        tx = onramp.functions.deposit(amount, wallet).build_transaction({
            "from": wallet,
            "nonce": nonce,
            "gas": 200000,
            "gasPrice": w3.eth.gas_price,
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"TX enviada: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        print(f"Deposito confirmado! Status: {receipt['status']}")
        time.sleep(3)

        # Verificar nuevo balance pUSD
        pusd_bal = pusd_contract.functions.balanceOf(wallet).call()
        print(f"pUSD ahora: {pusd_bal/1e6:.4f}\n")
    else:
        print("ERROR: no hay suficiente USDC ni pUSD para operar")
        return

    # 5. Aprobar pUSD para CTF
    print("Paso 3: Aprobaciones para trading...")
    approve_token(PUSD, CTF, "pUSD", "CTF", wallet, account)
    time.sleep(2)

    # 6. Aprobar CTF tokens para CTF Exchange
    approve_token(CTF, CTF_EXCHANGE, "CTF", "CTF_Exchange", wallet, account)
    time.sleep(2)

    # 7. Aprobar CTF tokens para Neg Risk Exchange
    approve_token(CTF, NEG_RISK_EX, "CTF", "NegRisk_Exchange", wallet, account)
    time.sleep(2)

    # 8. Verificar balance final
    print("\nBalances finales:")
    pusd_final = pusd_contract.functions.balanceOf(wallet).call()
    usdc_final = usdc_contract.functions.balanceOf(wallet).call()
    print(f"  USDC: {usdc_final/1e6:.4f}")
    print(f"  pUSD: {pusd_final/1e6:.4f}")

    print("\nListo! Ahora corre: python3 main.py")

if __name__ == "__main__":
    main()
