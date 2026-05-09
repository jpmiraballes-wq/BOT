from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=True)


def mask(v: str | None) -> str:
    if not v:
        return "MISSING"
    return f"OK len={len(v)} start={v[:6]} end={v[-4:]}"


def main() -> None:
    print("--- ENV CHECK ---")
    for k in [
        "PRIVATE_KEY",
        "FUNDER_ADDRESS",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "LIVE_DERIVE_API_CREDS",
        "LIVE_DRY_RUN",
        "LIVE_MAKER_ENABLED",
        "LIVE_ALLOW_SELL",
    ]:
        print(k, "=", mask(os.getenv(k)))

    pk = os.getenv("PRIVATE_KEY")
    funder = os.getenv("FUNDER_ADDRESS")
    if not pk or not funder:
        raise SystemExit("Missing PRIVATE_KEY or FUNDER_ADDRESS")
    if not pk.startswith("0x") or len(pk) != 66:
        raise SystemExit("PRIVATE_KEY format looks wrong. Expected 0x + 64 hex chars")
    if not funder.startswith("0x") or len(funder) != 42:
        raise SystemExit("FUNDER_ADDRESS format looks wrong. Expected 0x + 40 hex chars")

    print("--- SDK IMPORT CHECK ---")
    from py_clob_client_v2 import ApiCreds, ClobClient

    print("SDK imports OK")

    host = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com").rstrip("/")
    chain_id = int(float(os.getenv("POLYMARKET_CHAIN_ID", "137")))
    signature_type = int(float(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1")))

    creds = None
    if all(os.getenv(k) for k in ["POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"]):
        creds = ApiCreds(
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        )
        print("Using existing L2 API creds from .env")

    print("--- CLIENT BUILD CHECK ---")
    client = ClobClient(
        host=host,
        chain_id=chain_id,
        key=pk,
        creds=creds,
        signature_type=signature_type,
        funder=funder,
    )
    print("Client built OK", {"host": host, "chain_id": chain_id, "signature_type": signature_type, "funder": mask(funder)})

    if creds is None and os.getenv("LIVE_DERIVE_API_CREDS", "").lower() in {"1", "true", "yes", "on"}:
        print("--- DERIVE API CREDS CHECK ---")
        derive = (
            getattr(client, "create_or_derive_api_creds", None)
            or getattr(client, "create_or_derive_api_key", None)
            or getattr(client, "createOrDeriveApiKey", None)
        )
        if not derive:
            raise SystemExit("SDK has no create_or_derive API credential method")
        derived = derive()
        print("Derived creds OK", {"api_key": mask(getattr(derived, "api_key", None)), "api_secret": mask(getattr(derived, "api_secret", None)), "api_passphrase": mask(getattr(derived, "api_passphrase", None))})
        print("NOTE: this only derives/authenticates credentials. It does NOT post orders.")

    print("--- AUTH CHECK DONE ---")


if __name__ == "__main__":
    main()
