from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from py_clob_client_v2 import ApiCreds, ClobClient, OpenOrderParams, TradeParams

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)


def jdump(x: Any) -> str:
    return json.dumps(x, indent=2, ensure_ascii=False, default=str)


def build_client() -> ClobClient:
    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
    )

    return ClobClient(
        host=os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com"),
        chain_id=int(float(os.getenv("POLYMARKET_CHAIN_ID", "137"))),
        key=os.getenv("PRIVATE_KEY"),
        creds=creds,
        signature_type=int(float(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))),
        funder=os.getenv("FUNDER_ADDRESS"),
    )


def read_recent_order_ids(limit: int = 30) -> list[dict]:
    path = ROOT / "data" / "live_maker_orders.jsonl"
    if not path.exists():
        return []

    rows = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            row = json.loads(line)
        except Exception:
            continue
        res = row.get("response") or {}
        oid = res.get("orderID")
        if oid:
            rows.append(
                {
                    "orderID": oid,
                    "request": row.get("request") or {},
                    "submitted_at": row.get("submitted_at"),
                }
            )
    return rows


def main() -> None:
    client = build_client()

    recent = read_recent_order_ids()
    print("--- RECENT LOCAL ORDER IDS ---")
    print("count =", len(recent))
    for r in recent[-10:]:
        req = r["request"]
        print(
            {
                "orderID": r["orderID"],
                "token": req.get("token_id_short"),
                "side": req.get("side"),
                "price": req.get("price"),
                "size_usd": req.get("size_usd"),
                "size_shares": req.get("size_shares"),
                "question": req.get("question"),
            }
        )

    print("\n--- GET_ORDER STATUS ---")
    order_statuses = []
    for r in recent[-10:]:
        oid = r["orderID"]
        try:
            status = client.get_order(oid)
            order_statuses.append({"orderID": oid, "status": status})
            print("orderID =", oid)
            print(jdump(status))
        except Exception as e:
            order_statuses.append({"orderID": oid, "error": repr(e)})
            print("orderID =", oid, "ERROR =", repr(e))

    print("\n--- OPEN ORDERS ---")
    try:
        open_orders = client.get_open_orders(OpenOrderParams(), only_first_page=True)
        print("open_orders_count =", len(open_orders) if isinstance(open_orders, list) else "unknown")
        print(jdump(open_orders[:10] if isinstance(open_orders, list) else open_orders))
    except Exception as e:
        open_orders = []
        print("OPEN ORDERS ERROR =", repr(e))

    print("\n--- RECENT TRADES ---")
    try:
        trades = client.get_trades(TradeParams(), only_first_page=True)
        print("trades_count =", len(trades) if isinstance(trades, list) else "unknown")
        print(jdump(trades[:20] if isinstance(trades, list) else trades))
    except Exception as e:
        trades = []
        print("TRADES ERROR =", repr(e))

    out = {
        "recent_orders_count": len(recent),
        "order_statuses": order_statuses,
        "open_orders": open_orders,
        "trades": trades,
    }
    out_path = ROOT / "data" / "live_maker_fill_audit.json"
    out_path.write_text(jdump(out))
    print("\n--- WROTE ---")
    print(out_path)


if __name__ == "__main__":
    main()
