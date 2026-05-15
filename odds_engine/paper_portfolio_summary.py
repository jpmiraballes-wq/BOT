from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TRADES_PATH = DATA_DIR / "papertrade.jsonl"
MARKS_PATH = DATA_DIR / "papertrade_marks.jsonl"
OUT_PATH = DATA_DIR / "paper_portfolio_summary.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue

    return rows


def trade_key(row: dict) -> str:
    return "|".join([
        str(row.get("external_event_id") or ""),
        str(row.get("token_id") or ""),
        str(row.get("side") or ""),
    ])


def latest_open_unique_trades(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        if row.get("status") != "open":
            continue
        grouped[trade_key(row)].append(row)

    unique: list[dict] = []
    for _, group in grouped.items():
        group_sorted = sorted(group, key=lambda r: str(r.get("opened_at") or ""))
        first = dict(group_sorted[0])
        first["duplicate_open_rows"] = len(group_sorted)
        unique.append(first)

    return sorted(unique, key=lambda r: str(r.get("opened_at") or ""))


def latest_marks_by_trade(rows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}

    for row in rows:
        tid = str(row.get("trade_id") or "")
        if not tid:
            continue

        prev = latest.get(tid)
        if not prev or str(row.get("marked_at") or "") > str(prev.get("marked_at") or ""):
            latest[tid] = row

    return latest


def fnum(value, default=0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def main() -> None:
    trade_rows = read_jsonl(TRADES_PATH)
    mark_rows = read_jsonl(MARKS_PATH)

    open_trades = latest_open_unique_trades(trade_rows)
    marks = latest_marks_by_trade(mark_rows)

    positions = []
    total_exposure = 0.0
    total_unrealized = 0.0
    total_duplicate_rows = 0

    for trade in open_trades:
        tid = str(trade.get("id") or "")
        mark = marks.get(tid, {})

        size_usd = fnum(trade.get("size_usd"))
        pnl_usd = fnum(mark.get("unrealized_pnl_usd"))
        pnl_pct = fnum(mark.get("unrealized_pnl_pct"))
        duplicate_rows = int(trade.get("duplicate_open_rows") or 1)

        total_exposure += size_usd
        total_unrealized += pnl_usd
        total_duplicate_rows += max(0, duplicate_rows - 1)

        positions.append({
            "trade_id": tid,
            "signal_id": trade.get("signal_id"),
            "external_event_id": trade.get("external_event_id"),
            "polymarket_market_id": trade.get("polymarket_market_id"),
            "token_id": trade.get("token_id"),
            "side": trade.get("side"),
            "status": trade.get("status"),
            "entry_price": fnum(trade.get("entry_price")),
            "mark_price": mark.get("mark_price"),
            "price_source": mark.get("price_source"),
            "size_usd": size_usd,
            "quantity": fnum(trade.get("quantity")),
            "unrealized_pnl_usd": pnl_usd,
            "unrealized_pnl_pct": pnl_pct,
            "opened_at": trade.get("opened_at"),
            "last_marked_at": mark.get("marked_at"),
            "duplicate_open_rows": duplicate_rows,
            "reason_open": trade.get("reason_open"),
            "mark_status": mark.get("status") or "missing_mark",
        })

    best = max(positions, key=lambda p: p["unrealized_pnl_usd"], default=None)
    worst = min(positions, key=lambda p: p["unrealized_pnl_usd"], default=None)

    summary = {
        "generated_at": now_iso(),
        "source_files": {
            "trades": str(TRADES_PATH),
            "marks": str(MARKS_PATH),
        },
        "counts": {
            "raw_trade_rows": len(trade_rows),
            "raw_mark_rows": len(mark_rows),
            "open_unique_positions": len(positions),
            "duplicate_open_rows_extra": total_duplicate_rows,
        },
        "portfolio": {
            "total_exposure_usd": round(total_exposure, 6),
            "total_unrealized_pnl_usd": round(total_unrealized, 6),
            "total_unrealized_pnl_pct_on_exposure": round((total_unrealized / total_exposure) * 100, 4) if total_exposure else 0.0,
        },
        "best_position": best,
        "worst_position": worst,
        "positions": positions,
    }

    OUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(json.dumps({
        "ok": True,
        "summary_path": str(OUT_PATH),
        "open_unique_positions": len(positions),
        "total_exposure_usd": summary["portfolio"]["total_exposure_usd"],
        "total_unrealized_pnl_usd": summary["portfolio"]["total_unrealized_pnl_usd"],
        "total_unrealized_pnl_pct_on_exposure": summary["portfolio"]["total_unrealized_pnl_pct_on_exposure"],
        "duplicate_open_rows_extra": total_duplicate_rows,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
