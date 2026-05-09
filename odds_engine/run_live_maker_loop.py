from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from live_maker_executor import run_live_maker_once

load_dotenv(Path(".env"), override=True)

CYCLES = int(float(os.getenv("LIVE_LOOP_CYCLES", "3")))
SLEEP_SECONDS = float(os.getenv("LIVE_CYCLE_SECONDS", "10"))

def main() -> None:
    results = []

    print("--- LIVE MAKER LOOP START ---")
    print("cycles =", CYCLES)
    print("sleep_seconds =", SLEEP_SECONDS)
    print("dry_run =", os.getenv("LIVE_DRY_RUN"))
    print("allow_sell =", os.getenv("LIVE_ALLOW_SELL"))

    for i in range(CYCLES):
        print(f"\n--- CYCLE {i + 1}/{CYCLES} ---")
        result = run_live_maker_once()
        compact = {
            "cycle": i + 1,
            "dry_run": result.get("dry_run"),
            "orders_planned": result.get("orders_planned"),
            "orders_submitted": result.get("orders_submitted"),
            "orders_failed": result.get("orders_failed"),
            "planned_notional_usd": result.get("planned_notional_usd"),
        }
        print(json.dumps(compact, indent=2, default=str))
        results.append(compact)

        if i < CYCLES - 1:
            time.sleep(SLEEP_SECONDS)

    out = {
        "cycles": CYCLES,
        "sleep_seconds": SLEEP_SECONDS,
        "results": results,
    }
    Path("data/live_maker_loop_summary.json").write_text(json.dumps(out, indent=2, default=str))
    print("\n--- LIVE MAKER LOOP DONE ---")
    print(json.dumps(out, indent=2, default=str))

if __name__ == "__main__":
    main()
