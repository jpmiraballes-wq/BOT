#!/usr/bin/env python3
"""
Deposita USDC nativo a Polymarket via Bridge API oficial
Convierte automaticamente USDC -> pUSD sin necesitar la web
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

USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
PUSD        = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")

ERC20_ABI = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

TRANSFER_ABI = [
    {"inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
] + ERC20_ABI

def main():
    if not w3.is_connected():
        print("ERROR: no se pudo conectar al RPC")
        return

    print(f"RPC conectado (block {w3.eth.block_number})\n")

    wallet = Web3.to_checksum_address(WALLET_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)

    usdc = w3.eth.contract(address=USDC_NATIVE, abi=TRANSFER_ABI)
    pusd = w3.eth.contract(address=PUSD, abi=ERC20_ABI)

    usdc_bal = usdc.functions.balanceOf(wallet).call()
    pusd_bal = pusd.functions.balanceOf(wallet).call()

    print(f"Balance actual:")
    print(f"  USDC nativo: {usdc_bal/1e6:.4f}")
    print(f"  pUSD:        {pusd_bal/1e6:.4f}\n")

    if pusd_bal > 1_000_000:
        print(f"Ya tienes {pusd_bal/1e6:.2f} pUSD. Listo para operar.\n")
        print("Corre: python3 main.py")
        return

    if usdc_bal < 1_000_000:
        print("ERROR: necesitas al menos 1 USDC nativo")
        return

    # Paso 1: Obtener direccion de deposito del Bridge API
    print("Paso 1: Obteniendo direccion de deposito del Bridge API...")
    try:
        resp = requests.post(
            "https://bridge.polymarket.com/deposit",
            json={"address": wallet},
            timeout=15
        )
        print(f"  Bridge API response: {resp.status_code}")
        data = resp.json()
        print(f"  Data: {data}")
    except Exception as e:
        print(f"  Error Bridge API: {e}")
        data = {}

    # Buscar la direccion EVM de deposito
    deposit_address = None
    if "evm" in data:
        deposit_address = data["evm"]
    elif "addresses" in data:
        deposit_address = data["addresses"].get("evm") or data["addresses"].get("polygon")
    elif isinstance(data, dict):
        for key in ["evm", "polygon", "address"]:
            if key in data:
                deposit_address = data[key]
                break

    if not deposit_address:
        print(f"\nNo se pudo obtener direccion. Respuesta completa: {data}")
        print("\nPlan B: enviando USDC directamente a tu misma wallet para activar pUSD...")
        # Intento alternativo: usar el contrato Onramp correcto
        # segun docs, USDC se convierte a pUSD automaticamente al enviarlo
        print("\nConsulta la documentacion en: https://docs.polymarket.com/trading/bridge/deposit")
        return

    print(f"\n  Direccion de deposito EVM: {deposit_address}")

    # Paso 2: Enviar USDC a la direccion de deposito
    amount = int(usdc_bal * 0.95)  # 95% dejando gas
    print(f"\nPaso 2: Enviando {amount/1e6:.2f} USDC a la direccion de deposito...")

    nonce = w3.eth.get_transaction_count(wallet)
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(deposit_address),
        amount
    ).build_transaction({
        "from": wallet,
        "nonce": nonce,
        "gas": 100000,
        "gasPrice": w3.eth.gas_price,
        "chainId": 137,
    })
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"TX enviada: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    print(f"Confirmada! Status: {receipt['status']}")

    if receipt['status'] == 1:
        print(f"\nUSDA enviado. Esperando conversion a pUSD (puede tardar 1-2 min)...")
        for i in range(6):
            time.sleep(20)
            pusd_nuevo = pusd.functions.balanceOf(wallet).call()
            print(f"  [{i+1}/6] pUSD: {pusd_nuevo/1e6:.4f}")
            if pusd_nuevo > 1_000_000:
                print(f"\npUSD recibido! {pusd_nuevo/1e6:.2f} pUSD")
                print("Ahora corre: python3 main.py")
                return

        print("\nEl bridge puede tardar unos minutos mas.")
        print("Verifica en: https://bridge.polymarket.com/status/" + wallet)
        print("Cuando tengas pUSD, corre: python3 main.py")
    else:
        print("ERROR: la transaccion fallo")

if __name__ == "__main__":
    main()
