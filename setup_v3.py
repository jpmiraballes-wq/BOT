#!/usr/bin/env python3
"""
Setup v3 - Aprobaciones correctas para Polymarket CLOB
CTF es ERC1155 -> usa setApprovalForAll, no allowance/approve
"""
import os
import time
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
RPC = "https://polygon-mainnet.g.alchemy.com/v2/Jfo7UHHxiaq3qduY1XKhW"

w3 = Web3(Web3.HTTPProvider(RPC))

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
]

CTF_ABI = [
    {"inputs":[{"name":"operator","type":"address"},{"name":"approved","type":"bool"}],"name":"setApprovalForAll","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"},{"name":"operator","type":"address"}],"name":"isApprovedForAll","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"},
]

def send_tx(tx, account):
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    return tx_hash, receipt

def main():
    if not w3.is_connected():
        print("ERROR: no conectado al RPC")
        return

    print(f"Conectado (block {w3.eth.block_number})")
    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    usdc = w3.eth.contract(address=USDC_NATIVE, abi=ERC20_ABI)
    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
    ctf  = w3.eth.contract(address=CTF, abi=CTF_ABI)

    usdc_bal = usdc.functions.balanceOf(wallet).call()
    pusd_bal = pusd.functions.balanceOf(wallet).call()
    print(f"\nUSCC: {usdc_bal/1e6:.4f} | pUSD: {pusd_bal/1e6:.4f}\n")

    # === APROBACION 1: pUSD -> CTF Exchange (ERC20) ===
    print("1) pUSD -> CTF_EXCHANGE (ERC20 approve)...")
    try:
        al = pusd.functions.allowance(wallet, CTF_EXCHANGE).call()
        print(f"   allowance actual: {al/1e6:.2f}")
        if al < MAX_UINT // 2:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = pusd.functions.approve(CTF_EXCHANGE, MAX_UINT).build_transaction({
                "from": wallet, "nonce": nonce, "gas": 100000,
                "gasPrice": w3.eth.gas_price, "chainId": 137,
            })
            h, r = send_tx(tx, account)
            print(f"   OK status={r['status']} tx={h.hex()[:16]}...")
        else:
            print("   Ya aprobado")
    except Exception as e:
        print(f"   Error: {e}")

    time.sleep(2)

    # === APROBACION 2: pUSD -> NEG_RISK_EX (ERC20) ===
    print("2) pUSD -> NEG_RISK_EXCHANGE (ERC20 approve)...")
    try:
        al = pusd.functions.allowance(wallet, NEG_RISK_EX).call()
        print(f"   allowance actual: {al/1e6:.2f}")
        if al < MAX_UINT // 2:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = pusd.functions.approve(NEG_RISK_EX, MAX_UINT).build_transaction({
                "from": wallet, "nonce": nonce, "gas": 100000,
                "gasPrice": w3.eth.gas_price, "chainId": 137,
            })
            h, r = send_tx(tx, account)
            print(f"   OK status={r['status']} tx={h.hex()[:16]}...")
        else:
            print("   Ya aprobado")
    except Exception as e:
        print(f"   Error: {e}")

    time.sleep(2)

    # === APROBACION 3: CTF -> CTF_EXCHANGE (ERC1155 setApprovalForAll) ===
    print("3) CTF -> CTF_EXCHANGE (ERC1155 setApprovalForAll)...")
    try:
        approved = ctf.functions.isApprovedForAll(wallet, CTF_EXCHANGE).call()
        print(f"   isApprovedForAll: {approved}")
        if not approved:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = ctf.functions.setApprovalForAll(CTF_EXCHANGE, True).build_transaction({
                "from": wallet, "nonce": nonce, "gas": 100000,
                "gasPrice": w3.eth.gas_price, "chainId": 137,
            })
            h, r = send_tx(tx, account)
            print(f"   OK status={r['status']} tx={h.hex()[:16]}...")
        else:
            print("   Ya aprobado")
    except Exception as e:
        print(f"   Error: {e}")

    time.sleep(2)

    # === APROBACION 4: CTF -> NEG_RISK_EX (ERC1155 setApprovalForAll) ===
    print("4) CTF -> NEG_RISK_EXCHANGE (ERC1155 setApprovalForAll)...")
    try:
        approved = ctf.functions.isApprovedForAll(wallet, NEG_RISK_EX).call()
        print(f"   isApprovedForAll: {approved}")
        if not approved:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = ctf.functions.setApprovalForAll(NEG_RISK_EX, True).build_transaction({
                "from": wallet, "nonce": nonce, "gas": 100000,
                "gasPrice": w3.eth.gas_price, "chainId": 137,
            })
            h, r = send_tx(tx, account)
            print(f"   OK status={r['status']} tx={h.hex()[:16]}...")
        else:
            print("   Ya aprobado")
    except Exception as e:
        print(f"   Error: {e}")

    time.sleep(2)

    # === APROBACION 5: CTF -> NEG_RISK_AD (ERC1155 setApprovalForAll) ===
    print("5) CTF -> NEG_RISK_ADAPTER (ERC1155 setApprovalForAll)...")
    try:
        approved = ctf.functions.isApprovedForAll(wallet, NEG_RISK_AD).call()
        print(f"   isApprovedForAll: {approved}")
        if not approved:
            nonce = w3.eth.get_transaction_count(wallet)
            tx = ctf.functions.setApprovalForAll(NEG_RISK_AD, True).build_transaction({
                "from": wallet, "nonce": nonce, "gas": 100000,
                "gasPrice": w3.eth.gas_price, "chainId": 137,
            })
            h, r = send_tx(tx, account)
            print(f"   OK status={r['status']} tx={h.hex()[:16]}...")
        else:
            print("   Ya aprobado")
    except Exception as e:
        print(f"   Error: {e}")

    print("\n=== APROBACIONES LISTAS ===")
    print("Ahora corre: python3 main.py")

if __name__ == "__main__":
    main()
