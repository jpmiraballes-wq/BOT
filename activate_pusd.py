#!/usr/bin/env python3
"""
Deposita USDC nativo a Polymarket via Bridge API oficial
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

# Contratos oficiales
USDC_NATIVE  = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
PUSD         = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
CTF          = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
CTF_EXCHANGE = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EX  = Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")
NEG_RISK_AD  = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")

MAX_UINT = 2**256 - 1

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
]

# CTF es ERC1155 - usa setApprovalForAll, no approve
CTF_ABI = [
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
]

def approve_erc20(token_addr, spender_addr, name, wallet, account):
    """Aprueba un token ERC20 estandar"""
    token = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
    try:
        allowance = token.functions.allowance(wallet, spender_addr).call()
        balance = token.functions.balanceOf(wallet).call()
        print(f"  {name}: balance={balance/1e6:.4f}, allowance={allowance/1e6:.4f}")
        if allowance >= MAX_UINT // 2:
            print(f"  {name} ya aprobado (MAX)")
            return
        print(f"  Aprobando {name}...")
        nonce = w3.eth.get_transaction_count(wallet)
        tx = token.functions.approve(spender_addr, MAX_UINT).build_transaction({
            "from": wallet, "nonce": nonce, "gas": 100000,
            "gasPrice": w3.eth.gas_price, "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"  OK! TX: {tx_hash.hex()[:20]}...")
    except Exception as e:
        print(f"  Error en {name}: {e}")

def approve_erc1155(token_addr, operator_addr, name, wallet, account):
    """Aprueba un token ERC1155 (CTF) con setApprovalForAll"""
    token = w3.eth.contract(address=token_addr, abi=CTF_ABI)
    try:
        is_approved = token.functions.isApprovedForAll(wallet, operator_addr).call()
        print(f"  CTF -> {name}: approved={is_approved}")
        if is_approved:
            print(f"  CTF -> {name} ya aprobado")
            return
        print(f"  Aprobando CTF para {name}...")
        nonce = w3.eth.get_transaction_count(wallet)
        tx = token.functions.setApprovalForAll(operator_addr, True).build_transaction({
            "from": wallet, "nonce": nonce, "gas": 100000,
            "gasPrice": w3.eth.gas_price, "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        print(f"  OK! TX: {tx_hash.hex()[:20]}...")
    except Exception as e:
        print(f"  Error en CTF -> {name}: {e}")

def main():
    if not w3.is_connected():
        print("ERROR: no se pudo conectar al RPC")
        return

    print(f"RPC conectado (block {w3.eth.block_number})\n")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    usdc = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)

    usdc_bal = usdc.functions.balanceOf(wallet).call()
    pusd_bal = pusd.functions.balanceOf(wallet).call()

    print(f"Balances actuales:")
    print(f"  USDC nativo: {usdc_bal/1e6:.4f}")
    print(f"  pUSD:        {pusd_bal/1e6:.4f}\n")

    # --- PASO 1: Depositar USDC -> pUSD via Bridge API si no tenemos pUSD ---
    if pusd_bal > 1_000_000:
        print(f"Ya tienes {pusd_bal/1e6:.2f} pUSD. Saltando deposito.\n")
    elif usdc_bal > 1_000_000:
        print("Paso 1: Obteniendo direccion de deposito via Bridge API...")
        try:
            resp = requests.post(
                "https://bridge.polymarket.com/deposit",
                json={"address": wallet},
                timeout=15
            )
            data = resp.json()
            print(f"  Respuesta Bridge: {data}")

            # Buscar direccion EVM
            deposit_address = None
            if isinstance(data, dict):
                deposit_address = (data.get("evm") or 
                                   data.get("polygon") or
                                   (data.get("addresses") or {}).get("evm") or
                                   (data.get("addresses") or {}).get("polygon"))

            if deposit_address:
                amount = int(usdc_bal * 0.95)
                print(f"\n  Enviando {amount/1e6:.2f} USDC a {deposit_address}...")
                nonce = w3.eth.get_transaction_count(wallet)
                tx = usdc.functions.transfer(
                    Web3.to_checksum_address(deposit_address), amount
                ).build_transaction({
                    "from": wallet, "nonce": nonce, "gas": 100000,
                    "gasPrice": w3.eth.gas_price, "chainId": 137,
                })
                signed = account.sign_transaction(tx)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                print(f"  TX: {tx_hash.hex()}")
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
                print(f"  Status: {receipt['status']}")

                if receipt['status'] == 1:
                    print("  Esperando conversion a pUSD (2 min max)...")
                    for i in range(6):
                        time.sleep(20)
                        pusd_bal = pusd.functions.balanceOf(wallet).call()
                        print(f"  [{i+1}/6] pUSD: {pusd_bal/1e6:.4f}")
                        if pusd_bal > 1_000_000:
                            break
            else:
                print(f"  No se obtuvo direccion de deposito. Data: {data}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print("ERROR: no hay suficiente USDC ni pUSD")
        return

    # Recheck pUSD
    pusd_bal = pusd.functions.balanceOf(wallet).call()
    print(f"\npUSD disponible: {pusd_bal/1e6:.4f}")

    # --- PASO 2: Aprobaciones para trading ---
    print("\nPaso 2: Aprobaciones para trading...")

    # pUSD -> CTF (ERC20 approve)
    approve_erc20(PUSD, CTF, "pUSD->CTF", wallet, account)
    time.sleep(2)

    # CTF -> CTF_EXCHANGE (ERC1155 setApprovalForAll)
    approve_erc1155(CTF, CTF_EXCHANGE, "CTF_Exchange", wallet, account)
    time.sleep(2)

    # CTF -> NEG_RISK_EX (ERC1155 setApprovalForAll)
    approve_erc1155(CTF, NEG_RISK_EX, "NegRisk_Exchange", wallet, account)
    time.sleep(2)

    # CTF -> NEG_RISK_AD (ERC1155 setApprovalForAll)
    approve_erc1155(CTF, NEG_RISK_AD, "NegRisk_Adapter", wallet, account)
    time.sleep(2)

    print("\n=== Setup completo ===")
    pusd_final = pusd.functions.balanceOf(wallet).call()
    print(f"pUSD final: {pusd_final/1e6:.4f}")
    print("\nAhora corre: python3 main.py")

if __name__ == "__main__":
    main()
