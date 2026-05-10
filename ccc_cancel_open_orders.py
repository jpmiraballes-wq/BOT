#!/usr/bin/env python3
"""
Cancel all open Polymarket CLOB orders using the same local environment as odds_engine.

Usage from repo root:
    odds_engine/.venv/bin/python -u ccc_cancel_open_orders.py --real

This script intentionally does only one thing: cancel outstanding open orders.
It does not sell positions and does not place new orders.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default


def _mask(v: str | None) -> str | None:
    if not v:
        return None
    if len(v) <= 8:
        return "***"
    return v[:4] + "..." + v[-4:]


def _as_list(x: Any) -> list[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    if isinstance(x, dict):
        for key in ("data", "orders", "results"):
            if isinstance(x.get(key), list):
                return x[key]
        return [x]
    return [x]


def _order_id(o: Any) -> str | None:
    if isinstance(o, str):
        return o
    if isinstance(o, dict):
        for k in ("id", "orderID", "orderId", "order_id", "hash"):
            v = o.get(k)
            if v:
                return str(v)
    return None


def _call_get_orders(client: Any) -> list[Any]:
    # Try native no-arg call first.
    try:
        return _as_list(client.get_orders())
    except Exception as e1:
        last = e1

    # Then try py-clob-client OpenOrderParams if available.
    try:
        from py_clob_client.clob_types import OpenOrderParams  # type: ignore
        try:
            return _as_list(client.get_orders(OpenOrderParams()))
        except Exception as e2:
            last = e2
    except Exception:
        pass

    raise RuntimeError(f"get_orders failed: {last!r}")


def _cancel_all_direct(client: Any) -> Any:
    if hasattr(client, "cancel_all"):
        return client.cancel_all()
    raise AttributeError("client has no cancel_all")


def _cancel_ids(client: Any, ids: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not ids:
        return out

    if hasattr(client, "cancel_orders"):
        try:
            r = client.cancel_orders(ids)
            return [{"method": "cancel_orders", "ids": ids, "response": r}]
        except Exception as e:
            out.append({"method": "cancel_orders", "ok": False, "error": repr(e)})

    if hasattr(client, "cancel"):
        for oid in ids:
            try:
                out.append({"method": "cancel", "id": oid, "ok": True, "response": client.cancel(oid)})
            except Exception as e:
                out.append({"method": "cancel", "id": oid, "ok": False, "error": repr(e)})
        return out

    raise AttributeError("client has neither cancel_orders nor cancel")


def _build_client() -> Any:
    # Load env from common repo locations.
    root = Path.cwd()
    for p in [root / ".env", root / "odds_engine" / ".env", root / "live.env", root / "odds_engine" / "live.env"]:
        _load_dotenv(p)

    host = _env("POLYMARKET_CLOB_HOST", "CLOB_HOST", default="https://clob.polymarket.com")
    key = _env("POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY", "PK")
    chain_id = int(_env("POLYMARKET_CHAIN_ID", "CHAIN_ID", default="137") or "137")
    signature_type_raw = _env("POLYMARKET_SIGNATURE_TYPE", "SIGNATURE_TYPE", default="1")
    signature_type = int(signature_type_raw or "1")
    funder = _env("POLYMARKET_FUNDER", "FUNDER", "PROXY_WALLET", "PROXY_WALLET_ADDRESS")

    api_key = _env("CLOB_API_KEY", "POLYMARKET_API_KEY")
    api_secret = _env("CLOB_SECRET", "CLOB_API_SECRET", "POLYMARKET_API_SECRET")
    api_passphrase = _env("CLOB_PASS_PHRASE", "CLOB_PASSPHRASE", "CLOB_API_PASSPHRASE", "POLYMARKET_API_PASSPHRASE")

    print("CANCEL_ENV", json.dumps({
        "host": host,
        "chain_id": chain_id,
        "signature_type": signature_type,
        "funder": _mask(funder),
        "private_key": _mask(key),
        "api_key": _mask(api_key),
        "api_secret": _mask(api_secret),
        "api_passphrase": _mask(api_passphrase),
    }, indent=2))

    from py_clob_client.client import ClobClient  # type: ignore

    kwargs: dict[str, Any] = {"host": host, "chain_id": chain_id}
    if key:
        kwargs["key"] = key
    if signature_type is not None:
        kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder

    client = ClobClient(**kwargs)

    if api_key and api_secret and api_passphrase:
        try:
            from py_clob_client.clob_types import ApiCreds  # type: ignore
            client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
            print("AUTH: set_api_creds(ApiCreds) OK")
        except Exception as e:
            print("AUTH: ApiCreds failed", repr(e))
            try:
                client.set_api_creds({"api_key": api_key, "api_secret": api_secret, "api_passphrase": api_passphrase})
                print("AUTH: set_api_creds(dict) OK")
            except Exception as e2:
                print("AUTH: set_api_creds(dict) failed", repr(e2))
    else:
        # Last resort: derive or create creds from private key if the client supports it.
        for meth in ("derive_api_key", "create_or_derive_api_creds", "create_api_key"):
            if hasattr(client, meth):
                try:
                    creds = getattr(client, meth)()
                    if hasattr(client, "set_api_creds") and creds:
                        client.set_api_creds(creds)
                    print(f"AUTH: {meth} OK")
                    break
                except Exception as e:
                    print(f"AUTH: {meth} failed", repr(e))

    return client


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="Actually cancel orders. Without this, only lists them.")
    args = ap.parse_args()

    client = _build_client()
    orders = _call_get_orders(client)
    ids = [x for x in (_order_id(o) for o in orders) if x]

    print("OPEN_ORDERS_COUNT", len(orders))
    print("OPEN_ORDER_IDS_COUNT", len(ids))
    if ids:
        print("OPEN_ORDER_IDS", json.dumps(ids[:200], indent=2))

    if not args.real:
        print("DRY_RUN: not cancelling. Re-run with --real to cancel.")
        return 0

    if not ids:
        print("CANCEL_OK nothing_to_cancel")
        return 0

    try:
        r = _cancel_all_direct(client)
        print("CANCEL_ALL_RESPONSE", json.dumps(r, indent=2, default=str))
    except Exception as e:
        print("CANCEL_ALL_DIRECT_FAILED", repr(e))
        r = _cancel_ids(client, ids)
        print("CANCEL_IDS_RESPONSE", json.dumps(r, indent=2, default=str))

    # Verify after cancel.
    try:
        remaining = _call_get_orders(client)
        print("VERIFY_OPEN_ORDERS_REMAINING", len(remaining))
    except Exception as e:
        print("VERIFY_FAILED", repr(e))

    print("CANCEL_SCRIPT_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
