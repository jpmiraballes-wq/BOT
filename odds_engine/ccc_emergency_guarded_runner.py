#!/usr/bin/env python3
"""
Emergency guarded Polymarket runner.

Purpose:
- Never leave GTC/open orders hanging between cycles.
- Default mode is defensive: cancel open orders + run exit only.
- Micro real mode is intentionally tiny and must be explicitly requested.
- Uses only existing local scripts through .venv/bin/python.

Run from /Users/juanmiraballes/BOT/odds_engine.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parent
PY = ROOT / ".venv" / "bin" / "python"
DATA = ROOT / "data"

KILL_PATTERNS = [
    "true_stable_runner.py",
    "stable_maker_no_cancel.py",
    "live_sports_maker.py",
    "REAL_100",
    "HARDENED",
    "NO_REPEAT",
    "EXIT_ONLY",
]

SAFE_ENV_DEFAULTS = {
    # Micro buy limits. Existing scripts may ignore some vars, but these are harmless and useful if supported.
    "LIVE_DRY_RUN": "1",
    "LIVE_MAX_ORDER_USD": "2.00",
    "LIVE_MAX_CYCLE_NOTIONAL_USD": "2.00",
    "LIVE_MAX_MARKETS": "1",
    "LIVE_MAX_POSITIONS": "3",
    "LIVE_MAX_OPEN_POSITIONS": "3",
    "LIVE_MAX_MARKET_EXPOSURE_USD": "3.00",
    "LIVE_DAILY_LOSS_LIMIT_USD": "8.00",
    "LIVE_GAMMA_LIMIT": "1",
    "LIVE_ALLOW_DOUBLE_SIDE": "0",
    "LIVE_NO_REPEAT_MARKET": "1",
    "LIVE_CANCEL_AFTER_SECONDS": "8",
    "LIVE_POST_ONLY": "0",
    "LIVE_FORCE_TAKER": "1",
}


def log(*xs: object) -> None:
    print(*xs, flush=True)


def ensure_layout() -> None:
    os.chdir(ROOT)
    DATA.mkdir(exist_ok=True)
    if not PY.exists():
        raise SystemExit(f"NO_VENV_PYTHON: {PY}")
    for name in ["ccc_cancel_open_orders.py", "live_exit_data_api.py"]:
        if not (ROOT / name).exists():
            raise SystemExit(f"MISSING_REQUIRED_SCRIPT: {name}")


def run(cmd: Sequence[str], *, timeout: int | None = None, env: dict[str, str] | None = None, allow_fail: bool = False) -> int:
    log("$", " ".join(cmd))
    p = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True, timeout=timeout)
    if p.returncode != 0 and not allow_fail:
        raise SystemExit(f"COMMAND_FAILED rc={p.returncode}: {' '.join(cmd)}")
    return p.returncode


def quiet_kill_old() -> None:
    # Best-effort only. The user can also run pkill outside. Avoid killing this runner by not matching its filename.
    for pat in KILL_PATTERNS:
        subprocess.run(["pkill", "-f", pat], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cancel_all() -> None:
    run([str(PY), "-u", "ccc_cancel_open_orders.py", "--real"], timeout=90, allow_fail=True)


def exit_once(hold: float) -> None:
    if not (ROOT / "live_exit_data_api.py").exists():
        log("EXIT_SKIP missing live_exit_data_api.py")
        return
    run([str(PY), "-u", "live_exit_data_api.py", "--real", "--hold", str(hold)], timeout=180, allow_fail=True)


def maker_once(real: bool, timeout: int) -> None:
    # Prefer the wrapper if present; it should call live_sports_maker.py.
    script = "stable_maker_no_cancel.py" if (ROOT / "stable_maker_no_cancel.py").exists() else "live_sports_maker.py"
    env = os.environ.copy()
    env.update(SAFE_ENV_DEFAULTS)
    if real:
        env["LIVE_DRY_RUN"] = "0"
        env["CCC_EMERGENCY_MICRO_REAL"] = "1"
        cmd = [str(PY), "-u", script, "--real"]
    else:
        env["LIVE_DRY_RUN"] = "1"
        env["CCC_EMERGENCY_MICRO_DRY"] = "1"
        cmd = [str(PY), "-u", script]
    log("MAKER_SCRIPT", script, "REAL", real)
    run(cmd, timeout=timeout, env=env, allow_fail=True)


def compile_check() -> None:
    targets = [
        "ccc_emergency_guarded_runner.py",
        "ccc_cancel_open_orders.py",
        "live_exit_data_api.py",
        "stable_maker_no_cancel.py",
        "live_sports_maker.py",
        "live_risk_preflight.py",
    ]
    for t in targets:
        if (ROOT / t).exists():
            run([str(PY), "-m", "py_compile", t], timeout=60, allow_fail=False)
            log("PY_COMPILE_OK", t)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["defense", "micro-dry", "micro-real"], default="defense")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=20.0)
    ap.add_argument("--exit-hold", type=float, default=2.0)
    ap.add_argument("--maker-timeout", type=int, default=90)
    ap.add_argument("--kill-old", action="store_true")
    ap.add_argument("--compile", action="store_true")
    args = ap.parse_args()

    ensure_layout()
    if args.kill_old:
        quiet_kill_old()
    if args.compile:
        compile_check()

    log("CCC_EMERGENCY_RUNNER_START", "mode=", args.mode, "cycles=", args.cycles)
    for i in range(1, args.cycles + 1):
        log("################################################################################")
        log(f"CYCLE {i}/{args.cycles} mode={args.mode} ts={time.strftime('%Y-%m-%d %H:%M:%S')}")
        log("################################################################################")

        # Always clear stale open orders before doing anything.
        cancel_all()

        # Always give exit a chance first. This is portfolio defense.
        exit_once(args.exit_hold)

        if args.mode in {"micro-dry", "micro-real"}:
            maker_once(real=(args.mode == "micro-real"), timeout=args.maker_timeout)

            # Critical guard: maker may create GTC orders. Clear them immediately after the micro attempt.
            cancel_all()

            # Then run exit again, because a fill may have happened.
            exit_once(args.exit_hold)

            # Final clear: do not leave GTC orders open.
            cancel_all()

        if i < args.cycles:
            log("SLEEP", args.sleep)
            time.sleep(args.sleep)

    log("CCC_EMERGENCY_RUNNER_DONE")


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
