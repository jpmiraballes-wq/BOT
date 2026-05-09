from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from py_clob_client_v2 import ClobClient

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

load_dotenv(ENV_PATH, override=True)


def mask(v: str | None) -> str:
    if not v:
        return "MISSING"
    return f"OK len={len(v)} start={v[:6]} end={v[-4:]}"


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out: list[str] = []
    for line in lines:
        if "=" not in line or line.strip().startswith("#"):
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n")


def main() -> None:
    pk = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER_ADDRESS")
    if not pk or not funder:
        raise SystemExit("Missing PRIVATE_KEY or FUNDER_ADDRESS in .env")
    if not pk.startswith("0x") or len(pk) != 66:
        raise SystemExit("PRIVATE_KEY format wrong: expected 0x + 64 hex chars")
    if not funder.startswith("0x") or len(funder) != 42:
        raise SystemExit("FUNDER_ADDRESS format wrong: expected 0x + 40 hex chars")

    host = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/")
    chain_id = int(float(os.getenv("POLYMARKET_CHAIN_ID", "137")))
    signature_type = int(float(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")))

    print("--- DERIVE AND SAVE CLOB CREDS ---")
    client = ClobClient(host=host, chain_id=chain_id, key=pk, signature_type=signature_type, funder=funder)
    derive = (
        getattr(client, "create_or_derive_api_creds", None)
        or getattr(client, "create_or_derive_api_key", None)
        or getattr(client, "createOrDeriveApiKey", None)
    )
    if not derive:
        raise SystemExit("SDK has no create_or_derive API credential method")
    creds = derive()
    api_key = getattr(creds, "api_key", None)
    api_secret = getattr(creds, "api_secret", None)
    api_passphrase = getattr(creds, "api_passphrase", None)
    if not api_key or not api_secret or not api_passphrase:
        raise SystemExit("Derived creds incomplete")

    upsert_env(
        ENV_PATH,
        {
            "POLYMARKET_API_KEY": api_key,
            "POLYMARKET_API_SECRET": api_secret,
            "POLYMARKET_API_PASSPHRASE": api_passphrase,
            "LIVE_DERIVE_API_CREDS": "false",
        },
    )
    print("Saved to .env without printing secrets")
    print("POLYMARKET_API_KEY =", mask(api_key))
    print("POLYMARKET_API_SECRET =", mask(api_secret))
    print("POLYMARKET_API_PASSPHRASE =", mask(api_passphrase))
    print("LIVE_DERIVE_API_CREDS = false")


if __name__ == "__main__":
    main()
