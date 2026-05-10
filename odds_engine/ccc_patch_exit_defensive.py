#!/usr/bin/env python3
"""
Patch live_exit_data_api.py with defensive exposure exits + cooldown.

Design decisions:
- Keep wide sports disaster stop-loss defaults (-35 live / -30 unknown / -45 season).
- Do NOT panic-sell normal sports volatility.
- Add separate defensive-exposure exit for large concentrated losers only.
- Add cooldown so the same asset is not repeatedly defensive-sold every cycle.

This patcher modifies only plan_exit(), writes a timestamped backup, and py_compile-checks.
It is idempotent: running it again replaces the existing plan_exit with this version.
"""
from __future__ import annotations

import py_compile
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "live_exit_data_api.py"

NEW_FUNC = r'''def plan_exit(client, pos):
    import json
    import time as _time
    from pathlib import Path as _Path

    title = pos["title"] or ""
    kind = classify_market(title)

    min_profit = fenv("DATA_EXIT_MIN_PROFIT_PCT", 3.0)
    fast_profit = fenv("DATA_EXIT_FAST_PROFIT_PCT", 7.0)
    moon_profit = fenv("DATA_EXIT_MOON_PROFIT_PCT", 14.0)

    # Season: no salir por monedas en take-profit.
    if kind == "SEASON":
        min_profit = max(min_profit, fenv("DATA_EXIT_SEASON_MIN_PROFIT_PCT", 12.0))
        fast_profit = max(fast_profit, fenv("DATA_EXIT_SEASON_FAST_PROFIT_PCT", 25.0))
        moon_profit = max(moon_profit, fenv("DATA_EXIT_SEASON_MOON_PROFIT_PCT", 40.0))

    size = pos["size"] or 0
    avg = pos["avg_price"] or 0
    pct = pos["percent_pnl"] or 0
    initial_value = pos.get("initial_value")
    raw = pos.get("raw") or {}
    redeemable = bool(raw.get("redeemable"))
    asset_id = pos.get("asset_id")

    snap = book_prices(client, asset_id)
    best_bid = snap.get("best_bid")
    best_ask = snap.get("best_ask")

    plan = {
        **pos,
        "market_kind": kind,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "action": "SKIP",
        "reason": None,
        "sell_price": None,
        "sell_size": size,
        "expected_gross": None,
        "expected_pnl_vs_initial": None,
    }

    if not asset_id or size <= 0:
        plan["reason"] = "NO_SIZE_OR_ASSET"
        return plan

    # If Polymarket marks it redeemable and there is no live bid, do not spam sell attempts.
    if redeemable and best_bid is None:
        plan["reason"] = "REDEEMABLE_NO_LIVE_BID"
        return plan

    def _cooldown_path():
        return _Path(__file__).resolve().parent / "data" / "live_exit_cooldown.json"

    def _load_cooldowns():
        path = _cooldown_path()
        try:
            if path.exists():
                data = json.loads(path.read_text())
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _save_cooldowns(data):
        path = _cooldown_path()
        try:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except Exception:
            pass

    def _cooldown_active(key, seconds):
        if seconds <= 0:
            return False, 0
        data = _load_cooldowns()
        last = float(data.get(key, 0) or 0)
        left = seconds - (_time.time() - last)
        return left > 0, max(0, int(left))

    def _mark_cooldown(key):
        data = _load_cooldowns()
        data[key] = _time.time()
        # Keep file compact.
        cutoff = _time.time() - 86400
        data = {k: v for k, v in data.items() if float(v or 0) >= cutoff}
        _save_cooldowns(data)

    def set_sell(price, reason):
        sell_price = round(max(0.01, min(0.99, float(price))), 2)
        plan["action"] = "SELL"
        plan["reason"] = reason
        plan["sell_price"] = sell_price
        plan["expected_gross"] = round(size * sell_price, 6)
        if initial_value is not None:
            plan["expected_pnl_vs_initial"] = round(size * sell_price - initial_value, 6)
        if reason.startswith("DEFENSIVE_EXPOSURE_"):
            _mark_cooldown(f"defensive:{asset_id}")
        return plan

    # CTO WIDE STOP LOSS: keep the sports-volatility philosophy.
    # These are disaster stops, not normal exit triggers.
    stop_live = fenv("DATA_EXIT_STOP_LIVE_PCT", -35.0)
    stop_unknown = fenv("DATA_EXIT_STOP_UNKNOWN_PCT", -30.0)
    stop_season = fenv("DATA_EXIT_STOP_SEASON_PCT", -45.0)

    if kind == "SEASON":
        stop_loss = stop_season
    elif kind == "LIVE":
        stop_loss = stop_live
    else:
        stop_loss = stop_unknown

    if pct <= stop_loss:
        if best_bid is None:
            plan["reason"] = "STOP_LOSS_NO_VALID_BID"
            return plan
        if best_bid < 0.02:
            plan["reason"] = "STOP_LOSS_BID_TOO_LOW"
            return plan
        return set_sell(best_bid, f"STOP_LOSS_{kind}_SELL_AT_BID")

    # DEFENSIVE EXPOSURE EXIT:
    # This is NOT a global tight stop. It only cuts large concentrated losers with a valid bid.
    # It must happen before PNL_BELOW_THRESHOLD and BID_BELOW_AVG, because those are take-profit guards.
    exposure_usd = float(initial_value or 0)
    bid_ok = best_bid is not None
    ask_ok = best_ask is not None
    spread = None
    if bid_ok and ask_ok:
        try:
            spread = float(best_ask) - float(best_bid)
        except Exception:
            spread = None

    defensive_min_exposure_live = fenv("DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_LIVE_USD", 25.0)
    defensive_min_exposure_unknown = fenv("DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_UNKNOWN_USD", 35.0)
    defensive_min_exposure_season = fenv("DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_SEASON_USD", 999.0)
    defensive_loss_live = fenv("DATA_EXIT_DEFENSIVE_LOSS_LIVE_PCT", -15.0)
    defensive_loss_unknown = fenv("DATA_EXIT_DEFENSIVE_LOSS_UNKNOWN_PCT", -18.0)
    defensive_loss_season = fenv("DATA_EXIT_DEFENSIVE_LOSS_SEASON_PCT", -35.0)
    defensive_min_bid = fenv("DATA_EXIT_DEFENSIVE_MIN_BID", 0.05)
    defensive_max_spread = fenv("DATA_EXIT_DEFENSIVE_MAX_SPREAD", 0.15)
    defensive_cooldown_seconds = fenv("DATA_EXIT_DEFENSIVE_COOLDOWN_SECONDS", 900.0)

    if kind == "SEASON":
        defensive_min_exposure = defensive_min_exposure_season
        defensive_loss = defensive_loss_season
    elif kind == "LIVE":
        defensive_min_exposure = defensive_min_exposure_live
        defensive_loss = defensive_loss_live
    else:
        defensive_min_exposure = defensive_min_exposure_unknown
        defensive_loss = defensive_loss_unknown

    if pct <= defensive_loss and exposure_usd >= defensive_min_exposure:
        active, left = _cooldown_active(f"defensive:{asset_id}", defensive_cooldown_seconds)
        if active:
            plan["reason"] = f"DEFENSIVE_COOLDOWN_ACTIVE_{left}s"
            return plan
        if not bid_ok:
            plan["reason"] = "DEFENSIVE_EXIT_NO_VALID_BID"
            return plan
        if float(best_bid) < defensive_min_bid:
            plan["reason"] = "DEFENSIVE_EXIT_BID_TOO_LOW"
            return plan
        if spread is not None and spread > defensive_max_spread:
            plan["reason"] = "DEFENSIVE_EXIT_SPREAD_TOO_WIDE"
            return plan
        return set_sell(best_bid, f"DEFENSIVE_EXPOSURE_{kind}_SELL_AT_BID")

    # From here down is TAKE PROFIT logic only. Losses must not be sold by normal profit rules.
    if pct < min_profit:
        plan["reason"] = "PNL_BELOW_THRESHOLD"
        return plan

    if best_bid is None:
        plan["reason"] = "NO_VALID_BID"
        return plan

    # Nunca vender por debajo del avg si estamos en take profit.
    if best_bid < avg:
        plan["reason"] = "BID_BELOW_AVG"
        return plan

    # Si no hay ask pero hay bid fuerte y profit grande, vendemos al bid.
    if best_ask is None:
        if pct >= fast_profit:
            sell_price = best_bid
            reason = "NO_ASK_FAST_SELL_AT_BID"
        else:
            plan["reason"] = "NO_VALID_ASK_FOR_NORMAL_SELL"
            return plan
    elif pct >= moon_profit:
        sell_price = best_bid
        reason = "MOON_SELL_AT_BID"
    elif pct >= fast_profit:
        sell_price = best_bid
        reason = "FAST_SELL_AT_BID"
    else:
        # Maker dentro del spread.
        sell_price = min(best_ask - 0.01, max(best_bid + 0.01, avg * (1 + min_profit / 100)))
        reason = "NORMAL_MAKER_SELL"

    sell_price = round(max(0.01, min(0.99, sell_price)), 2)

    if sell_price < avg:
        plan["reason"] = "SELL_PRICE_BELOW_AVG"
        return plan

    return set_sell(sell_price, reason)
'''


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"MISSING {TARGET}")
    src = TARGET.read_text()
    start = src.find("def plan_exit(client, pos):")
    if start < 0:
        raise SystemExit("NO_ENCUENTRO def plan_exit(client, pos):")
    end = src.find("\ndef place_sell", start)
    if end < 0:
        end = src.find("\ndef ", start + 10)
    if end < 0:
        raise SystemExit("NO_ENCUENTRO fin de plan_exit")

    backup = TARGET.with_name(TARGET.name + f".bak_defensive_exit_{int(time.time())}")
    shutil.copy2(TARGET, backup)
    TARGET.write_text(src[:start] + NEW_FUNC + src[end:])
    py_compile.compile(str(TARGET), doraise=True)
    print("PATCHED", TARGET)
    print("BACKUP", backup)
    print("PY_COMPILE_OK", TARGET.name)
    print("DEFENSIVE_EXIT_PATCH_OK")
    print("DEFAULTS: live defensive exit only if pnl<=-15%, exposure>=25 USD, bid>=0.05, spread<=0.15")
    print("DEFAULTS: original disaster stop kept at LIVE -35 / UNKNOWN -30 / SEASON -45")
    print("DEFAULTS: defensive cooldown 900 seconds per asset")


if __name__ == "__main__":
    main()
