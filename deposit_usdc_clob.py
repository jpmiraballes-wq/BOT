from web3 import Web3
from dotenv import load_dotenv
import os
load_dotenv()

ALCHEMY_KEY = os.getenv("ALCHEMY_KEY", "Jfo7UHHxiaq3qduY1XKhW")
w3 = Web3(Web3.HTTPProvider(f"https://polygon-mainnet.g.alchemy.com/v2/{ALCHEMY_KEY}"))
wallet = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS"))
account = w3.eth.account.from_key(os.getenv("PRIVATE_KEY"))

USDC = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
# Polymarket deposit proxy
DEPOSIT_PROXY = Web3.to_checksum_address("0xdFE02Eb6733538f8Ea35D585af8DE5958AD99E40")

erc20_abi = [
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]

usdc = w3.eth.contract(address=USDC, abi=erc20_abi)
bal = usdc.functions.balanceOf(wallet).call()
print(f"USDC on-chain: {bal/1e6:.4f}")

# Aprobar al deposit proxy
allowance = usdc.functions.allowance(wallet, DEPOSIT_PROXY).call()
print(f"Allowance al proxy: {allowance/1e6:.4f}")

if allowance < bal:
    print("Aprobando deposit proxy...")
    nonce = w3.eth.get_transaction_count(wallet)
    tx = usdc.functions.approve(DEPOSIT_PROXY, 2**256-1).build_transaction({
        "from": wallet, "nonce": nonce, "gas": 100000,
        "gasPrice": w3.eth.gas_price, "chainId": 137
    })
    signed = account.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
    print(f"Aprobacion: status={r['status']} tx={h.hex()[:20]}...")
else:
    print("Proxy ya aprobado OK")

# Ahora depositar via proxy
deposit_abi = [
    {"inputs":[{"name":"receiver","type":"address"},{"name":"amount","type":"uint256"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"}
]
proxy = w3.eth.contract(address=DEPOSIT_PROXY, abi=deposit_abi)
amount = int(bal * 0.9)  # depositar 90% del balance
print(f"Depositando {amount/1e6:.4f} USDC al CLOB...")
nonce = w3.eth.get_transaction_count(wallet)
tx = proxy.functions.deposit(wallet, amount).build_transaction({
    "from": wallet, "nonce": nonce, "gas": 200000,
    "gasPrice": w3.eth.gas_price, "chainId": 137
})
signed = account.sign_transaction(tx)
h = w3.eth.send_raw_transaction(signed.raw_transaction)
r = w3.eth.wait_for_transaction_receipt(h, timeout=60)
print(f"Deposito: status={r['status']} tx={h.hex()[:20]}...")
