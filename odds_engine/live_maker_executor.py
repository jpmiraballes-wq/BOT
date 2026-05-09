from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from config import settings

log = logging.getLogger("live_maker_executor")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@dataclass
class LiveLimits:
    max_order_usd: float
    max_cycle_notional_usd: float
    max_open_orders: int
    max_markets: int
    batch_size: int
    cycle_seconds: float
    cancel_before_post: bool
    dry_run: bool
    allow_sell: bool


class LiveMakerExecutor:
    """Guarded real-money maker executor.

    Uses existing maker output (`data/paper_maker_summary.json`) as the source of
    quotes. It can cancel and post real CLOB GTC post-only limit orders, but only
    when explicit local .env gates are enabled. No private keys should ever be
    committed or pasted into chat.
    """

    def __init__(self) -> None:
        self.data_dir = Path(settings.data_dir)
        self.summary_path = self.data_dir / "paper_maker_summary.json"
        self.live_log_path = self.data_dir / "live_maker_orders.jsonl"
        self.live_summary_path = self.data_dir / "live_maker_summary.json"
        self.host = os.getenv("POLYMARKET_CLOB_URL", settings.polymarket_clob_url).rstrip("/")
        self.chain_id = _int_env("POLYMARKET_CHAIN_ID", 137)
        self.limits = LiveLimits(
            max_order_usd=_float_env("LIVE_MAX_ORDER_USD", 1.0),
            max_cycle_notional_usd=_float_env("LIVE_MAX_CYCLE_NOTIONAL_USD", 10.0),
            max_open_orders=_int_env("LIVE_MAX_OPEN_ORDERS", 15),
            max_markets=_int_env("LIVE_MAX_MARKETS", 3),
            batch_size=min(15, _int_env("LIVE_BATCH_SIZE", 12)),
            cycle_seconds=_float_env("LIVE_CYCLE_SECONDS", 2.0),
            cancel_before_post=_bool_env("LIVE_CANCEL_BEFORE_POST", True),
            dry_run=_bool_env("LIVE_DRY_RUN", False),
            allow_sell=_bool_env("LIVE_ALLOW_SELL", False),
        )

    def _assert_armed(self) -> None:
        if not _bool_env("LIVE_MAKER_ENABLED", False):
            raise RuntimeError("LIVE_MAKER_ENABLED is not true")
        confirm = os.getenv("LIVE_CONFIRM_TEXT", "").strip()
        if confirm != "I_ACCEPT_REAL_MONEY_RISK":
            raise RuntimeError("LIVE_CONFIRM_TEXT must be exactly I_ACCEPT_REAL_MONEY_RISK")
        if not self.limits.dry_run:
            required = ["PRIVATE_KEY", "FUNDER_ADDRESS"]
            missing = [k for k in required if not os.getenv(k)]
            if missing:
                raise RuntimeError("Missing live env vars: " + ", ".join(missing))
            has_l2 = all(os.getenv(k) for k in ["POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"])
            if not has_l2 and not _bool_env("LIVE_DERIVE_API_CREDS", False):
                raise RuntimeError("Missing L2 creds. Set POLYMARKET_API_KEY/SECRET/PASSPHRASE or LIVE_DERIVE_API_CREDS=true")
        if self.limits.max_order_usd > 5:
            raise RuntimeError("LIVE_MAX_ORDER_USD > 5 blocked in V1")
        if self.limits.max_cycle_notional_usd > 50:
            raise RuntimeError("LIVE_MAX_CYCLE_NOTIONAL_USD > 50 blocked in V1")
        if self.limits.max_open_orders > 30:
            raise RuntimeError("LIVE_MAX_OPEN_ORDERS > 30 blocked in V1")

    def _client(self):
        try:
            from py_clob_client_v2 import ClobClient, ApiCreds
        except Exception as exc:
            raise RuntimeError("py-clob-client-v2 is not installed. Run: pip install -r requirements.txt") from exc

        creds = None
        if all(os.getenv(k) for k in ["POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"]):
            creds = ApiCreds(
                api_key=os.getenv("POLYMARKET_API_KEY"),
                api_secret=os.getenv("POLYMARKET_API_SECRET"),
                api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
            )

        client = ClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=os.getenv("PRIVATE_KEY"),
            creds=creds,
            signature_type=_int_env("POLYMARKET_SIGNATURE_TYPE", 1),
            funder=os.getenv("FUNDER_ADDRESS"),
        )

        if creds is None and _bool_env("LIVE_DERIVE_API_CREDS", False):
            derive = getattr(client, "create_or_derive_api_creds", None) or getattr(client, "create_or_derive_api_key", None) or getattr(client, "createOrDeriveApiKey", None)
            if not derive:
                raise RuntimeError("SDK has no API credential derivation method found")
            creds = derive()
            client = ClobClient(
                host=self.host,
                chain_id=self.chain_id,
                key=os.getenv("PRIVATE_KEY"),
                creds=creds,
                signature_type=_int_env("POLYMARKET_SIGNATURE_TYPE", 1),
                funder=os.getenv("FUNDER_ADDRESS"),
            )
        return client

    def _candidate_orders(self) -> list[dict]:
        summary = _read_json(self.summary_path)
        rows = summary.get("best_markets") or []
        orders: list[dict] = []
        seen_markets = set()
        cycle_notional = 0.0
        sides = [("BUY", "bid_quote")]
        if self.limits.allow_sell:
            sides.append(("SELL", "ask_quote"))
        for r in rows:
            if len(seen_markets) >= self.limits.max_markets:
                break
            token_id = str(r.get("token_id") or "")
            question = str(r.get("question") or "")
            if not token_id:
                continue
            seen_markets.add(str(r.get("question") or r.get("token_id_short") or token_id[:10]))
            for side, px_key in sides:
                price = round(_float(r.get(px_key)), 4)
                if price <= 0.01 or price >= 0.99:
                    continue
                if side == "BUY" and price >= _float(r.get("best_ask")):
                    continue
                if side == "SELL" and price <= _float(r.get("best_bid")):
                    continue
                usd = min(self.limits.max_order_usd, self.limits.max_cycle_notional_usd - cycle_notional)
                if usd < 0.5:
                    break
                size_shares = round(usd / price, 4)
                cycle_notional += usd
                orders.append({
                    "token_id": token_id,
                    "token_id_short": r.get("token_id_short") or token_id[:10],
                    "question": question,
                    "outcome": r.get("outcome"),
                    "side": side,
                    "price": price,
                    "size_usd": round(usd, 4),
                    "size_shares": size_shares,
                    "best_bid": r.get("best_bid"),
                    "best_ask": r.get("best_ask"),
                    "spread": r.get("spread"),
                })
                if len(orders) >= self.limits.batch_size or cycle_notional >= self.limits.max_cycle_notional_usd:
                    return orders
        return orders

    def _cancel_all(self, client) -> dict:
        if self.limits.dry_run:
            return {"dry_run": True, "action": "cancel_all"}
        fn = getattr(client, "cancel_all", None) or getattr(client, "cancelAll", None)
        if not fn:
            raise RuntimeError("SDK cancel_all method not found")
        return fn()

    def _post_one(self, client, order: dict) -> dict:
        if self.limits.dry_run:
            return {"dry_run": True, "accepted": True, "order": order}
        try:
            from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except Exception as exc:
            raise RuntimeError("SDK order classes not found") from exc

        side_value = BUY if order["side"] == "BUY" else SELL
        args = OrderArgs(
            token_id=order["token_id"],
            price=order["price"],
            size=order["size_shares"],
            side=side_value,
        )
        opts = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        fn = getattr(client, "create_and_post_order", None) or getattr(client, "createAndPostOrder", None)
        if fn:
            try:
                return fn(args, opts, "GTC", True)
            except TypeError:
                try:
                    return fn(args, opts)
                except TypeError:
                    return fn(args)
        create_fn = getattr(client, "create_order", None) or getattr(client, "createOrder", None)
        post_fn = getattr(client, "post_order", None) or getattr(client, "postOrder", None)
        if not create_fn or not post_fn:
            raise RuntimeError("SDK create/post order methods not found")
        signed = create_fn(args, opts)
        try:
            return post_fn(signed, "GTC", True)
        except TypeError:
            return post_fn(signed)

    def run_once(self) -> dict:
        self._assert_armed()
        client = None if self.limits.dry_run else self._client()
        orders = self._candidate_orders()
        result: dict[str, Any] = {
            "generated_at": _now_iso(),
            "mode": "LIVE_MAKER_V1",
            "dry_run": self.limits.dry_run,
            "live_orders_enabled": not self.limits.dry_run,
            "orders_planned": len(orders),
            "orders_submitted": 0,
            "orders_failed": 0,
            "max_order_usd": self.limits.max_order_usd,
            "max_cycle_notional_usd": self.limits.max_cycle_notional_usd,
            "allow_sell": self.limits.allow_sell,
            "planned_notional_usd": round(sum(_float(o.get("size_usd")) for o in orders), 6),
            "cancel_before_post": self.limits.cancel_before_post,
            "cancel_result": None,
            "orders": [],
        }
        if self.limits.cancel_before_post:
            result["cancel_result"] = self._cancel_all(client)
        for order in orders:
            row = {"submitted_at": _now_iso(), "request": order, "ok": False, "response": None, "error": None}
            try:
                row["response"] = self._post_one(client, order)
                row["ok"] = True
                result["orders_submitted"] += 1
            except Exception as exc:
                row["error"] = str(exc)
                result["orders_failed"] += 1
            result["orders"].append(row)
            _append_jsonl(self.live_log_path, row)
        self.live_summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        log.info("live_maker_v1 planned=%s submitted=%s failed=%s notional=%.4f dry_run=%s allow_sell=%s", result["orders_planned"], result["orders_submitted"], result["orders_failed"], result["planned_notional_usd"], result["dry_run"], result["allow_sell"])
        return result


def run_live_maker_once() -> dict:
    return LiveMakerExecutor().run_once()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(run_live_maker_once(), ensure_ascii=False, indent=2, default=str))
