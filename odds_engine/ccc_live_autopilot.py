#!/usr/bin/env python3
"""
CCC Live Autopilot for Polymarket odds_engine.

Goal:
- One command to run portfolio defense + real maker buying/selling cycles.
- Patch local live_exit_data_api.py defensively before start.
- Never leave stale GTC orders hanging forever.
- Allow many orders, but under explicit risk caps and cooldowns.

This wrapper intentionally does not rewrite your trading model. It orchestrates the local scripts already present
on the Mac and injects risk environment variables most of those scripts already understand.

Run from /Users/juanmiraballes/BOT/odds_engine after git pull:
    .venv/bin/python -u ccc_live_autopilot.py --real --cycles 999999 --sleep 45 --kill-old --compile

Safer first run:
    .venv/bin/python -u ccc_live_autopilot.py --dry --cycles 2 --sleep 10 --kill-old --compile
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "bin" / "python"
DATA = ROOT / "data"
STATUS_JSON = DATA / "ccc_live_autopilot_status.json"
STATUS_TXT = DATA / "ccc_live_autopilot_status.txt"

KILL_PATTERNS = [
    "true_stable_runner.py",
    "stable_maker_no_cancel.py",
    "live_sports_maker.py",
    "ccc_emergency_guarded_runner.py",
    "CCC_LIVE_AUTOPILOT_OLD",
    "REAL_100",
    "HARDENED",
    "NO_REPEAT",
    "EXIT_ONLY",
]

COMPILE_TARGETS = [
    "ccc_live_autopilot.py",
    "ccc_emergency_guarded_runner.py",
    "ccc_cancel_open_orders.py",
    "ccc_patch_exit_defensive.py",
    "live_exit_data_api.py",
    "stable_maker_no_cancel.py",
    "live_sports_maker.py",
    "live_risk_preflight.py",
]


def log(*xs: object) -> None:
    print(*xs, flush=True)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ensure_layout() -> None:
    os.chdir(ROOT)
    DATA.mkdir(exist_ok=True)
    if not PY.exists():
        raise SystemExit(f"NO_VENV_PYTHON: {PY}")
    for name in ["ccc_cancel_open_orders.py", "live_exit_data_api.py"]:
        if not (ROOT / name).exists():
            raise SystemExit(f"MISSING_REQUIRED_SCRIPT: {name}")


def run(
    cmd: Sequence[str],
    *,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    allow_fail: bool = True,
) -> int:
    log("$", " ".join(cmd))
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, timeout=timeout)
        if p.returncode != 0 and not allow_fail:
            raise SystemExit(f"COMMAND_FAILED rc={p.returncode}: {' '.join(cmd)}")
        return int(p.returncode)
    except subprocess.TimeoutExpired:
        log("COMMAND_TIMEOUT", " ".join(cmd), "timeout=", timeout)
        if not allow_fail:
            raise
        return 124


def quiet_kill_old() -> None:
    # Best effort; avoid matching ccc_live_autopilot.py itself.
    for pat in KILL_PATTERNS:
        subprocess.run(["pkill", "-f", pat], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def compile_check() -> None:
    for t in COMPILE_TARGETS:
        if (ROOT / t).exists():
            run([str(PY), "-m", "py_compile", t], timeout=60, allow_fail=False)
            log("PY_COMPILE_OK", t)


def patch_exit_defensive() -> None:
    patcher = ROOT / "ccc_patch_exit_defensive.py"
    if patcher.exists():
        run([str(PY), "-u", str(patcher.name)], timeout=90, allow_fail=False)
    else:
        log("PATCH_SKIP missing ccc_patch_exit_defensive.py")


def cancel_all() -> int:
    return run([str(PY), "-u", "ccc_cancel_open_orders.py", "--real"], timeout=90, allow_fail=True)


def maybe_preflight(env: dict[str, str]) -> int:
    if not (ROOT / "live_risk_preflight.py").exists():
        log("PREFLIGHT_SKIP missing live_risk_preflight.py")
        return 0
    # Some versions do not accept flags. Keep it plain.
    return run([str(PY), "-u", "live_risk_preflight.py"], timeout=120, env=env, allow_fail=True)


def exit_once(hold: float, env: dict[str, str]) -> int:
    return run([str(PY), "-u", "live_exit_data_api.py", "--real", "--hold", str(hold)], timeout=240, env=env, allow_fail=True)


def choose_maker_script() -> str | None:
    for name in ["stable_maker_no_cancel.py", "live_sports_maker.py"]:
        if (ROOT / name).exists():
            return name
    return None


def maker_once(real: bool, timeout: int, env: dict[str, str]) -> int:
    script = choose_maker_script()
    if not script:
        log("MAKER_SKIP no stable_maker_no_cancel.py / live_sports_maker.py found")
        return 2
    cmd = [str(PY), "-u", script]
    if real:
        cmd.append("--real")
    log("MAKER_SCRIPT", script, "REAL", real)
    return run(cmd, timeout=timeout, env=env, allow_fail=True)


def build_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()

    # Buy/order risk controls. These names intentionally cover several historical variants.
    caps = {
        "LIVE_DRY_RUN": "0" if args.real else "1",
        "CCC_LIVE_AUTOPILOT": "1",
        "CCC_LIVE_AUTOPILOT_REAL": "1" if args.real else "0",
        "LIVE_MAX_ORDER_USD": str(args.max_order_usd),
        "LIVE_MAX_CYCLE_NOTIONAL_USD": str(args.max_cycle_notional_usd),
        "LIVE_MAX_SESSION_NOTIONAL_USD": str(args.max_session_notional_usd),
        "LIVE_MAX_MARKETS": str(args.max_markets),
        "LIVE_MAX_POSITIONS": str(args.max_positions),
        "LIVE_MAX_OPEN_POSITIONS": str(args.max_positions),
        "LIVE_MAX_MARKET_EXPOSURE_USD": str(args.max_market_exposure_usd),
        "LIVE_MAX_EVENT_EXPOSURE_USD": str(args.max_event_exposure_usd),
        "LIVE_DAILY_LOSS_LIMIT_USD": str(args.daily_loss_limit_usd),
        "LIVE_ALLOW_DOUBLE_SIDE": "1" if args.allow_double_side else "0",
        "LIVE_NO_REPEAT_MARKET": "1",
        "LIVE_CANCEL_AFTER_SECONDS": str(args.order_ttl),
        "LIVE_POST_ONLY": "0" if args.allow_taker else "1",
        "LIVE_FORCE_TAKER": "1" if args.allow_taker else "0",
        "LIVE_MIN_EDGE_PCT": str(args.min_edge_pct),
        "LIVE_MAX_SPREAD": str(args.max_spread),
        "LIVE_MIN_BID": str(args.min_bid),
        "LIVE_MIN_ASK": str(args.min_ask),
        "LIVE_MAX_ASK": str(args.max_ask),
        "LIVE_GAMMA_LIMIT": "1",
        # Exit controls.
        "DATA_EXIT_STOP_LIVE_PCT": str(args.stop_live_pct),
        "DATA_EXIT_STOP_UNKNOWN_PCT": str(args.stop_unknown_pct),
        "DATA_EXIT_STOP_SEASON_PCT": str(args.stop_season_pct),
        "DATA_EXIT_DEFENSIVE_LOSS_LIVE_PCT": str(args.defensive_loss_live_pct),
        "DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_LIVE_USD": str(args.defensive_min_exposure_live_usd),
        "DATA_EXIT_DEFENSIVE_COOLDOWN_SECONDS": str(args.defensive_cooldown_seconds),
        "DATA_EXIT_DEFENSIVE_MIN_BID": str(args.defensive_min_bid),
        "DATA_EXIT_DEFENSIVE_MAX_SPREAD": str(args.defensive_max_spread),
        "DATA_EXIT_MIN_PROFIT_PCT": str(args.take_profit_min_pct),
        "DATA_EXIT_FAST_PROFIT_PCT": str(args.take_profit_fast_pct),
        "DATA_EXIT_MOON_PROFIT_PCT": str(args.take_profit_moon_pct),
    }
    env.update(caps)
    return env


def write_status(state: dict[str, object]) -> None:
    DATA.mkdir(exist_ok=True)
    state = {**state, "updated_at": now()}
    STATUS_JSON.write_text(json.dumps(state, indent=2, sort_keys=True))
    lines = [
        f"updated_at: {state.get('updated_at')}",
        f"mode: {state.get('mode')}",
        f"cycle: {state.get('cycle')}/{state.get('cycles')}",
        f"real: {state.get('real')}",
        f"last_cancel_rc: {state.get('last_cancel_rc')}",
        f"last_preflight_rc: {state.get('last_preflight_rc')}",
        f"last_exit_rc: {state.get('last_exit_rc')}",
        f"last_maker_rc: {state.get('last_maker_rc')}",
        f"final_cancel_rc: {state.get('final_cancel_rc')}",
    ]
    STATUS_TXT.write_text("\n".join(lines) + "\n")


def sleep_with_status(seconds: float, state: dict[str, object]) -> None:
    if seconds <= 0:
        return
    log("SLEEP", seconds)
    state["sleeping_seconds"] = seconds
    write_status(state)
    time.sleep(seconds)


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="Real buy/sell mode.")
    mode.add_argument("--dry", action="store_true", help="Dry maker mode; exits still call live_exit_data_api with --real only if local script does so.")
    ap.add_argument("--cycles", type=int, default=999999)
    ap.add_argument("--sleep", type=float, default=45.0)
    ap.add_argument("--exit-hold", type=float, default=2.0)
    ap.add_argument("--maker-timeout", type=int, default=180)
    ap.add_argument("--order-ttl", type=float, default=45.0, help="Seconds to leave new GTC orders alive before cancel sweep.")
    ap.add_argument("--post-exit-cancel-delay", type=float, default=8.0)
    ap.add_argument("--kill-old", action="store_true")
    ap.add_argument("--compile", action="store_true")
    ap.add_argument("--no-patch", action="store_true")
    ap.add_argument("--skip-preflight", action="store_true")

    # Real buy caps. Defaults are intentionally useful but not insane.
    ap.add_argument("--max-order-usd", type=float, default=7.0)
    ap.add_argument("--max-cycle-notional-usd", type=float, default=45.0)
    ap.add_argument("--max-session-notional-usd", type=float, default=250.0)
    ap.add_argument("--max-markets", type=int, default=8)
    ap.add_argument("--max-positions", type=int, default=40)
    ap.add_argument("--max-market-exposure-usd", type=float, default=18.0)
    ap.add_argument("--max-event-exposure-usd", type=float, default=35.0)
    ap.add_argument("--daily-loss-limit-usd", type=float, default=35.0)
    ap.add_argument("--allow-double-side", action="store_true")
    ap.add_argument("--allow-taker", action="store_true", help="Allow taker/marketable orders if local maker supports it.")
    ap.add_argument("--min-edge-pct", type=float, default=2.0)
    ap.add_argument("--max-spread", type=float, default=0.08)
    ap.add_argument("--min-bid", type=float, default=0.02)
    ap.add_argument("--min-ask", type=float, default=0.03)
    ap.add_argument("--max-ask", type=float, default=0.97)

    # Exit defaults. Keep your 35% disaster stop logic.
    ap.add_argument("--stop-live-pct", type=float, default=-35.0)
    ap.add_argument("--stop-unknown-pct", type=float, default=-30.0)
    ap.add_argument("--stop-season-pct", type=float, default=-45.0)
    ap.add_argument("--defensive-loss-live-pct", type=float, default=-15.0)
    ap.add_argument("--defensive-min-exposure-live-usd", type=float, default=25.0)
    ap.add_argument("--defensive-cooldown-seconds", type=float, default=900.0)
    ap.add_argument("--defensive-min-bid", type=float, default=0.05)
    ap.add_argument("--defensive-max-spread", type=float, default=0.15)
    ap.add_argument("--take-profit-min-pct", type=float, default=3.0)
    ap.add_argument("--take-profit-fast-pct", type=float, default=7.0)
    ap.add_argument("--take-profit-moon-pct", type=float, default=14.0)
    args = ap.parse_args()

    if args.dry:
        args.real = False

    ensure_layout()
    if args.kill_old:
        quiet_kill_old()
    if not args.no_patch:
        patch_exit_defensive()
    if args.compile:
        compile_check()

    env = build_env(args)
    state: dict[str, object] = {
        "mode": "real" if args.real else "dry",
        "real": bool(args.real),
        "cycles": args.cycles,
        "started_at": now(),
        "max_order_usd": args.max_order_usd,
        "max_cycle_notional_usd": args.max_cycle_notional_usd,
        "max_markets": args.max_markets,
        "order_ttl": args.order_ttl,
    }
    write_status(state)

    log("CCC_LIVE_AUTOPILOT_START", "mode=", state["mode"], "cycles=", args.cycles)
    log("RISK_CAPS", json.dumps({k: env[k] for k in sorted(env) if k.startswith("LIVE_MAX_") or k in {"LIVE_DRY_RUN", "LIVE_POST_ONLY", "LIVE_FORCE_TAKER"}}, sort_keys=True))

    for i in range(1, args.cycles + 1):
        log("################################################################################")
        log(f"CYCLE {i}/{args.cycles} mode={state['mode']} ts={now()}")
        log("################################################################################")

        state.update({"cycle": i, "last_maker_rc": None})
        state["last_cancel_rc"] = cancel_all()

        if not args.skip_preflight:
            state["last_preflight_rc"] = maybe_preflight(env)
        else:
            state["last_preflight_rc"] = "SKIP"

        # Portfolio defense and profit exits before new buys.
        state["last_exit_rc"] = exit_once(args.exit_hold, env)
        if args.post_exit_cancel_delay > 0:
            log("POST_EXIT_CANCEL_DELAY", args.post_exit_cancel_delay)
            time.sleep(args.post_exit_cancel_delay)
        state["post_exit_cancel_rc"] = cancel_all()

        # Maker buying/selling. In dry mode, local maker should not place real orders due LIVE_DRY_RUN=1.
        state["last_maker_rc"] = maker_once(args.real, args.maker_timeout, env)

        # Keep orders alive briefly so maker orders can fill; then cancel leftovers.
        if args.order_ttl > 0:
            log("ORDER_TTL_WAIT", args.order_ttl)
            time.sleep(args.order_ttl)
        state["post_maker_cancel_rc"] = cancel_all()

        # A fill may have happened; run exits again to take quick profit / protect portfolio.
        state["second_exit_rc"] = exit_once(args.exit_hold, env)
        if args.post_exit_cancel_delay > 0:
            log("FINAL_EXIT_CANCEL_DELAY", args.post_exit_cancel_delay)
            time.sleep(args.post_exit_cancel_delay)
        state["final_cancel_rc"] = cancel_all()

        write_status(state)
        if i < args.cycles:
            sleep_with_status(args.sleep, state)

    log("CCC_LIVE_AUTOPILOT_DONE")
    write_status({**state, "done": True})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("INTERRUPTED")
        try:
            cancel_all()
        except Exception:
            pass
        raise
