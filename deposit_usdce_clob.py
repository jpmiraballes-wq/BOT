from web3 import Web3
from dotenv import load_dotenv
import os
load_dotenv()

ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "Jfo7UHHxiaq3qduY1XKhW")
w3 = Web3(Web3.HTTPProvider(f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"))
wallet = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))
account = w3.eth.account.from_key(os.getenv("PRIVATE_KEY"))

USDCe = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
CLOB_EXCHANGE = Web3.to_checksum_address("0xC5d563A36AE78145C45a50134d48A1215220f80a")

erc20_abi = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

usdce = w3.eth.contract(address=USDCe, abi=erc20_abi)
bal = usdce.functions.balanceOf(wallet).call()
print(f"USDC.e on-chain: ${bal/1e6:.4f}")

allowance = usdce.functions.allowance(wallet, CLOB_EXCHANGE).call()
print(f"Allowance CLOB:  ${allowance/1e6:.4f}")

if allowance < bal:
    print("Aprobando CLOB Exchange para USDC.e...")
    nonce = w3.eth.get_transaction_count(wallet)
    tx = usdce.functions.approve(CLOB_EXCHANGE, 2**256-1).build_transaction({
        "from": wallet, "nonce": nonce, "gas": 100000,
        "gasPrice": w3.eth.gas_price, "chainId": 137
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
    print(f"Aprobacion: status={r['status']} tx={h.hex()[:20]}...")
else:
    print("Allowance ya OK")

print(f"Listo! El CLOB deberia ver ${bal/1e6:.4f} USDC.e")
