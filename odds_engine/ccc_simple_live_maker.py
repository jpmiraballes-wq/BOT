#!/usr/bin/env python3
"""
CCC Simple Live Sports Maker.

Why this exists:
- The previous local maker was filtering every candidate as known/repeated:
  CCC_CANDIDATE_FILTER in=0 out=0 blocked_known=411
- This maker is intentionally simple, versioned, and auditable.
- It buys liquid sports markets only, under hard notional caps.

It does NOT touch exits. The autopilot still handles:
- cancel sweep
- live_risk_preflight
- live_exit_data_api
- cancel sweep after TTL

Run:
  .venv/bin/python -u ccc_simple_live_maker.py --real
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SUMMARY_PATH = DATA / "ccc_simple_live_maker_summary.json"

GAMMA = os.environ.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com/markets")

ALLOWED_WORDS = tuple(
    x.strip().lower()
    for x in os.environ.get(
        "CCC_ALLOWED_SPORTS_WORDS",
        "ufc,mma,nba,wnba,nhl,mlb,nfl,soccer,football,premier league,champions league,la liga,epl,serie a,bundesliga,ligue 1,world cup,tennis,golf,formula 1,f1",
    ).split(",")
    if x.strip()
)

BLOCKED_WORDS = tuple(
    x.strip().lower()
    for x in os.environ.get(
        "CCC_BLOCKED_WORDS",
        "election,elections,mayor,mayoral,president,presidential,politics,congress,senate,crypto,bitcoin,ethereum,solana,fed,cpi,trump,biden,putin,netanyahu",
    ).split(",")
    if x.strip()
)


def log(*xs: object) -> None:
    print(*xs, flush=True)


def load_dotenv(path: Path) -> None:
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


def env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v not in (None, ""):
            return v
    return default


def as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except Exception:
        return default


def mask(v: str | None) -> str | None:
    if not v:
        return None
    if len(v) <= 8:
        return "***"
    return v[:4] + "..." + v[-4:]


def parse_jsonish(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        try:
            x = json.loads(s)
            if isinstance(x, list):
                return x
            return [x]
        except Exception:
            return [p.strip().strip('"').strip("'") for p in s.split(",") if p.strip()]
    return [v]


def get_text(m: dict[str, Any]) -> str:
    parts: list[str] = []
    for k in ("question", "title", "slug", "eventSlug", "category", "subcategory", "description"):
        v = m.get(k)
        if v:
            parts.append(str(v))
    for tag in parse_jsonish(m.get("tags")):
        if isinstance(tag, dict):
            parts.append(str(tag.get("label") or tag.get("name") or ""))
        else:
            parts.append(str(tag))
    return " ".join(parts).lower()


def is_sports_market(m: dict[str, Any]) -> tuple[bool, str]:
    txt = get_text(m)
    if any(w in txt for w in BLOCKED_WORDS):
        return False, "blocked_word"
    if any(w in txt for w in ALLOWED_WORDS):
        return True, "allowed_word"
    return False, "not_sports"


def is_active_market(m: dict[str, Any]) -> tuple[bool, str]:
    for k in ("closed", "archived", "acceptingOrders"):
        if k in m:
            if k in ("closed", "archived") and bool(m.get(k)):
                return False, k
            if k == "acceptingOrders" and m.get(k) is False:
                return False, "not_accepting_orders"
    if str(m.get("active", "true")).lower() in {"false", "0"}:
        return False, "inactive"
    return True, "active"


def http_json(url: str, timeout: int = 20) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "ccc-simple-live-maker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_gamma_markets(limit: int) -> list[dict[str, Any]]:
    params = {
        "closed": "false",
        "active": "true",
        "archived": "false",
        "limit": str(limit),
        "offset": "0",
        "order": "volume24hr",
        "ascending": "false",
    }
    url = GAMMA + "?" + urllib.parse.urlencode(params)
    raw = http_json(url)
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("data", "markets", "results"):
            if isinstance(raw.get(key), list):
                return [x for x in raw[key] if isinstance(x, dict)]
    return []


def extract_outcomes(m: dict[str, Any]) -> list[dict[str, Any]]:
    names = parse_jsonish(m.get("outcomes"))
    token_ids = parse_jsonish(m.get("clobTokenIds") or m.get("clob_token_ids") or m.get("tokens"))
    prices = parse_jsonish(m.get("outcomePrices") or m.get("outcome_prices"))

    if token_ids and all(isinstance(x, dict) for x in token_ids):
        out = []
        for t in token_ids:
            tok = t.get("token_id") or t.get("tokenId") or t.get("id")
            name = t.get("outcome") or t.get("name") or t.get("title")
            if tok:
                out.append({"name": str(name or "Outcome"), "token_id": str(tok), "gamma_price": as_float(t.get("price"), 0.0)})
        return out

    out: list[dict[str, Any]] = []
    n = max(len(names), len(token_ids))
    for i in range(n):
        tok = token_ids[i] if i < len(token_ids) else None
        if not tok:
            continue
        name = names[i] if i < len(names) else f"Outcome {i}"
        gp = as_float(prices[i], 0.0) if i < len(prices) else 0.0
        out.append({"name": str(name), "token_id": str(tok), "gamma_price": gp})
    return out


def build_client() -> Any:
    for p in [ROOT / ".env", ROOT.parent / ".env", ROOT / "live.env", ROOT.parent / "live.env"]:
        load_dotenv(p)

    host = env("POLYMARKET_CLOB_HOST", "CLOB_HOST", default="https://clob.polymarket.com")
    key = env("POLYMARKET_PRIVATE_KEY", "PRIVATE_KEY", "PK")
    chain_id = int(env("POLYMARKET_CHAIN_ID", "CHAIN_ID", default="137") or "137")
    signature_type = int(env("POLYMARKET_SIGNATURE_TYPE", "SIGNATURE_TYPE", default="2") or "2")
    funder = env("POLYMARKET_FUNDER", "FUNDER", "PROXY_WALLET", "PROXY_WALLET_ADDRESS")

    api_key = env("CLOB_API_KEY", "POLYMARKET_API_KEY")
    api_secret = env("CLOB_SECRET", "CLOB_API_SECRET", "POLYMARKET_API_SECRET")
    api_passphrase = env("CLOB_PASS_PHRASE", "CLOB_PASSPHRASE", "CLOB_API_PASSPHRASE", "POLYMARKET_API_PASSPHRASE")

    log("SIMPLE_MAKER_ENV", json.dumps({
        "host": host,
        "chain_id": chain_id,
        "signature_type": signature_type,
        "funder": mask(funder),
        "private_key": mask(key),
        "api_key": mask(api_key),
        "api_secret": mask(api_secret),
        "api_passphrase": mask(api_passphrase),
    }, indent=2))

    from py_clob_client.client import ClobClient  # type: ignore

    kwargs: dict[str, Any] = {"host": host, "chain_id": chain_id}
    if key:
        kwargs["key"] = key
    kwargs["signature_type"] = signature_type
    if funder:
        kwargs["funder"] = funder
    client = ClobClient(**kwargs)

    if api_key and api_secret and api_passphrase:
        try:
            from py_clob_client.clob_types import ApiCreds  # type: ignore
            client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
            log("AUTH set_api_creds(ApiCreds) OK")
        except Exception as e:
            log("AUTH ApiCreds failed", repr(e))
            try:
                client.set_api_creds({"api_key": api_key, "api_secret": api_secret, "api_passphrase": api_passphrase})
                log("AUTH set_api_creds(dict) OK")
            except Exception as e2:
                log("AUTH dict failed", repr(e2))
    else:
        log("AUTH warning: no API creds in env; client may try private-key auth only")

    return client


def book_best_bid_ask(client: Any, token_id: str) -> tuple[float | None, float | None]:
    try:
        if hasattr(client, "get_order_book"):
            ob = client.get_order_book(token_id)
        else:
            ob = client.get_book(token_id)
    except Exception:
        return None, None

    def levels(name: str) -> list[Any]:
        if isinstance(ob, dict):
            return ob.get(name) or []
        return getattr(ob, name, []) or []

    bids = levels("bids")
    asks = levels("asks")

    def price(x: Any) -> float | None:
        try:
            if isinstance(x, dict):
                return float(x.get("price"))
            return float(getattr(x, "price"))
        except Exception:
            return None

    bid_prices = [p for p in (price(x) for x in bids) if p is not None]
    ask_prices = [p for p in (price(x) for x in asks) if p is not None]
    best_bid = max(bid_prices) if bid_prices else None
    best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


def place_buy(client: Any, token_id: str, price: float, shares: float, post_only: bool) -> dict[str, Any]:
    from py_clob_client.clob_types import OrderArgs, OrderType  # type: ignore
    try:
        from py_clob_client.order_builder.constants import BUY  # type: ignore
    except Exception:
        BUY = "BUY"

    args = OrderArgs(price=round(price, 3), size=round(shares, 4), side=BUY, token_id=str(token_id))
    signed = client.create_order(args)

    try:
        return client.post_order(signed, OrderType.GTC, post_only=post_only)
    except TypeError:
        return client.post_order(signed, OrderType.GTC)


def read_existing_exposure_usd() -> tuple[dict[str, float], dict[str, float]]:
    by_token: dict[str, float] = {}
    by_event: dict[str, float] = {}
    p = DATA / "live_portfolio_data_api.json"
    if not p.exists():
        return by_token, by_event
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return by_token, by_event
    positions = raw.get("positions") if isinstance(raw, dict) else raw
    if not isinstance(positions, list):
        return by_token, by_event
    for x in positions:
        if not isinstance(x, dict):
            continue
        tok = str(x.get("asset_id") or x.get("asset") or "")
        event = str(x.get("eventSlug") or x.get("event_slug") or x.get("eventId") or x.get("event_id") or "")
        val = as_float(x.get("current_value") or x.get("currentValue") or x.get("initial_value") or x.get("initialValue"), 0.0)
        if tok:
            by_token[tok] = by_token.get(tok, 0.0) + val
        if event:
            by_event[event] = by_event.get(event, 0.0) + val
    return by_token, by_event


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--limit", type=int, default=int(env("CCC_SIMPLE_GAMMA_LIMIT", default="250") or "250"))
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)

    dry_run = not args.real or as_bool(env("LIVE_DRY_RUN", default="0"), False)
    max_order_usd = as_float(env("LIVE_MAX_ORDER_USD", "MAX_ORDER_USD", default="7"), 7.0)
    max_cycle_usd = as_float(env("LIVE_MAX_CYCLE_NOTIONAL_USD", "MAX_CYCLE_NOTIONAL_USD", default="45"), 45.0)
    max_markets = int(as_float(env("LIVE_MAX_MARKETS", "MAX_MARKETS", default="8"), 8))
    max_market_exposure = as_float(env("LIVE_MAX_MARKET_EXPOSURE_USD", default="18"), 18.0)
    max_event_exposure = as_float(env("LIVE_MAX_EVENT_EXPOSURE_USD", default="35"), 35.0)
    max_spread = as_float(env("LIVE_MAX_SPREAD", default="0.08"), 0.08)
    min_ask = as_float(env("LIVE_MIN_ASK", default="0.03"), 0.03)
    max_ask = as_float(env("LIVE_MAX_ASK", default="0.97"), 0.97)
    post_only = as_bool(env("CCC_SIMPLE_POST_ONLY", default="0"), False)

    log("--- CCC SIMPLE LIVE MAKER START ---")
    log("dry_run =", dry_run)
    log("caps =", json.dumps({
        "max_order_usd": max_order_usd,
        "max_cycle_usd": max_cycle_usd,
        "max_markets": max_markets,
        "max_market_exposure": max_market_exposure,
        "max_event_exposure": max_event_exposure,
        "max_spread": max_spread,
        "min_ask": min_ask,
        "max_ask": max_ask,
        "post_only": post_only,
    }, sort_keys=True))

    client = build_client()
    markets = fetch_gamma_markets(args.limit)
    log("gamma_markets_raw =", len(markets))

    exposure_token, exposure_event = read_existing_exposure_usd()

    scanned = 0
    skipped: dict[str, int] = {}
    plans: list[dict[str, Any]] = []

    for m in markets:
        active_ok, active_reason = is_active_market(m)
        if not active_ok:
            skipped[active_reason] = skipped.get(active_reason, 0) + 1
            continue
        sports_ok, sports_reason = is_sports_market(m)
        if not sports_ok:
            skipped[sports_reason] = skipped.get(sports_reason, 0) + 1
            continue

        outs = extract_outcomes(m)
        if not outs:
            skipped["no_tokens"] = skipped.get("no_tokens", 0) + 1
            continue

        scanned += 1
        event_key = str(m.get("eventSlug") or m.get("slug") or m.get("conditionId") or m.get("id") or "")
        event_expo = exposure_event.get(event_key, 0.0)
        if event_expo >= max_event_exposure:
            skipped["event_exposure_cap"] = skipped.get("event_exposure_cap", 0) + 1
            continue

        best_local: dict[str, Any] | None = None
        for o in outs:
            token = o["token_id"]
            tok_expo = exposure_token.get(token, 0.0)
            if tok_expo >= max_market_exposure:
                continue
            bid, ask = book_best_bid_ask(client, token)
            if bid is None or ask is None:
                continue
            spread = ask - bid
            if spread < 0 or spread > max_spread:
                continue
            if ask < min_ask or ask > max_ask:
                continue

            score = (spread, abs(0.5 - ask))
            cand = {
                "market_id": str(m.get("id") or ""),
                "condition_id": str(m.get("conditionId") or ""),
                "event_slug": event_key,
                "question": str(m.get("question") or m.get("title") or m.get("slug") or ""),
                "outcome": o["name"],
                "token_id": token,
                "bid": bid,
                "ask": ask,
                "spread": spread,
                "score": score,
                "existing_token_exposure": tok_expo,
                "existing_event_exposure": event_expo,
            }
            if best_local is None or score < best_local["score"]:
                best_local = cand

        if best_local:
            plans.append(best_local)
            if len(plans) >= max_markets:
                break

    submitted = 0
    failed = 0
    planned_notional = 0.0
    orders: list[dict[str, Any]] = []

    for p in plans:
        remaining = max_cycle_usd - planned_notional
        notional = min(max_order_usd, remaining)
        if notional < 1.0:
            break
        price = p["ask"] if not post_only else p["bid"]
        shares = notional / max(price, 0.001)
        p["order_price"] = round(price, 3)
        p["order_shares"] = round(shares, 4)
        p["order_notional_usd"] = round(price * shares, 4)

        if dry_run:
            orders.append({"plan": p, "ok": False, "dry_run": True, "response": None})
            planned_notional += p["order_notional_usd"]
            continue

        try:
            resp = place_buy(client, p["token_id"], price, shares, post_only)
            orders.append({"plan": p, "ok": True, "dry_run": False, "response": resp})
            submitted += 1
            planned_notional += p["order_notional_usd"]
            log("BUY_OK", p["question"], p["outcome"], "price=", price, "shares=", round(shares, 4), "resp=", resp)
        except Exception as e:
            failed += 1
            orders.append({"plan": p, "ok": False, "dry_run": False, "error": repr(e)})
            log("BUY_FAILED", p["question"], p["outcome"], repr(e))

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "CCC_SIMPLE_LIVE_MAKER",
        "dry_run": dry_run,
        "gamma_markets_raw": len(markets),
        "sports_scanned": scanned,
        "skipped": skipped,
        "orders_planned": len(plans),
        "orders_submitted": submitted,
        "orders_failed": failed,
        "planned_notional_usd": round(planned_notional, 4),
        "orders": orders,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2, default=str))
    log("--- CCC SIMPLE LIVE MAKER RESULT ---")
    log(json.dumps({k: v for k, v in summary.items() if k != "orders"}, indent=2, default=str))
    log("WROTE", SUMMARY_PATH)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
