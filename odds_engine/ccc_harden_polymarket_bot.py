#!/usr/bin/env python3
"""
CCC Polymarket bot hardening patch.
Run from /Users/juanmiraballes/BOT/odds_engine or from repo root.
It patches the local bot safely, with backups, compile checks, and idempotent guards.
"""
from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

MARK_START = "# === CCC HARDENING START ==="
MARK_END = "# === CCC HARDENING END ==="

LIVE_KEYS = [
    "LIVE_DRY_RUN",
    "LIVE_MAX_ORDER_USD",
    "LIVE_MAX_CYCLE_NOTIONAL_USD",
    "LIVE_MAX_MARKETS",
    "LIVE_GAMMA_LIMIT",
]


def find_project_dir() -> Path:
    here = Path.cwd().resolve()
    candidates = [here, here / "odds_engine", here.parent / "odds_engine"]
    for c in candidates:
        if (c / "live_sports_maker.py").exists() and (c / "stable_maker_no_cancel.py").exists():
            return c
    raise SystemExit("NO_ENCUENTRO odds_engine con live_sports_maker.py y stable_maker_no_cancel.py")


def backup(path: Path) -> Path:
    b = path.with_name(path.name + f".bak_CCC_HARDEN_{int(time.time())}")
    shutil.copy2(path, b)
    return b


def strip_old_hardening(s: str) -> str:
    s = re.sub(rf"\n?{re.escape(MARK_START)}.*?{re.escape(MARK_END)}\n?", "\n", s, flags=re.S)
    # Remove previous one-line inserts from earlier attempts.
    s = re.sub(r"^\s*planned\s*,\s*safe_skipped_repeat_markets\s*=\s*_ccc_[^\n]*\n", "", s, flags=re.M)
    s = re.sub(r"^\s*candidates\s*=\s*_ccc_filter_candidates\(candidates\)\n", "", s, flags=re.M)
    return s


HELPER = r'''
# === CCC HARDENING START ===
# Safety layer: portfolio-aware no-repeat, one side per market/event, and persistent blocklist.
# It is intentionally self-contained so it survives wrapper changes.
import json as _ccc_json
from pathlib import Path as _ccc_Path

_CCC_BLOCK_FILE = _ccc_Path("data/ccc_blocked_market_keys.json")
_CCC_POSITION_FILES = [
    _ccc_Path("data/live_portfolio_data_api.json"),
    _ccc_Path("data/live_exit_data_api_summary.json"),
    _ccc_Path("data/paper_portfolio_summary.json"),
]
_CCC_KEY_FIELDS = {
    "market", "condition_id", "conditionId", "market_slug", "marketSlug",
    "event_slug", "eventSlug", "eventId", "question", "title", "slug",
}
_CCC_TOKEN_FIELDS = {"token_id", "asset_id", "asset", "oppositeAsset"}


def _ccc_norm(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v or v.lower() in {"none", "null"}:
        return None
    return v


def _ccc_walk(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _ccc_walk(v)
    elif isinstance(x, list):
        for v in x:
            yield from _ccc_walk(v)


def _ccc_keys_from_obj(obj, include_tokens=False):
    keys = set()
    for d in _ccc_walk(obj):
        for k, v in d.items():
            if k in _CCC_KEY_FIELDS or (include_tokens and k in _CCC_TOKEN_FIELDS):
                nv = _ccc_norm(v)
                if nv:
                    keys.add(f"{k}:{nv}")
        # Extra normalized event key: title/question often catches opposite outcomes in same market.
        title = _ccc_norm(d.get("question") or d.get("title"))
        if title:
            keys.add("title:" + title.lower())
        cond = _ccc_norm(d.get("condition_id") or d.get("conditionId") or d.get("market"))
        if cond:
            keys.add("condition_or_market:" + cond)
        ev = _ccc_norm(d.get("event_slug") or d.get("eventSlug") or d.get("eventId"))
        if ev:
            keys.add("event:" + ev)
        slug = _ccc_norm(d.get("market_slug") or d.get("marketSlug") or d.get("slug"))
        if slug:
            keys.add("slug:" + slug)
    return keys


def _ccc_load_json(path):
    try:
        if path.exists() and path.stat().st_size:
            return _ccc_json.loads(path.read_text())
    except Exception:
        return None
    return None


def _ccc_load_blocked():
    data = _ccc_load_json(_CCC_BLOCK_FILE)
    if isinstance(data, list):
        return set(str(x) for x in data)
    if isinstance(data, dict):
        xs = data.get("keys") or data.get("blocked") or []
        return set(str(x) for x in xs)
    return set()


def _ccc_save_blocked(keys):
    try:
        _CCC_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CCC_BLOCK_FILE.write_text(_ccc_json.dumps(sorted(keys), indent=2, ensure_ascii=False))
    except Exception as e:
        print("CCC_BLOCK_SAVE_WARN", repr(e))


def _ccc_portfolio_keys():
    keys = set()
    for p in _CCC_POSITION_FILES:
        data = _ccc_load_json(p)
        if data is not None:
            keys |= _ccc_keys_from_obj(data, include_tokens=True)
    return keys


def _ccc_obj_has_valid_bookish_data(obj):
    # If book fields exist, avoid dead/no-book markets. If fields are absent, do not block just because wrapper omitted them.
    saw_book_field = False
    for d in _ccc_walk(obj):
        if "best_bid" in d or "best_ask" in d:
            saw_book_field = True
            bid = d.get("best_bid")
            ask = d.get("best_ask")
            try:
                if bid is not None and ask is not None and float(ask) > float(bid) >= 0:
                    return True
            except Exception:
                pass
    return not saw_book_field


def _ccc_filter_candidates(candidates):
    blocked = _ccc_load_blocked() | _ccc_portfolio_keys()
    safe = []
    skipped = 0
    seen_cycle = set()
    for c in candidates or []:
        keys = _ccc_keys_from_obj(c, include_tokens=True)
        if not keys:
            skipped += 1
            continue
        if not _ccc_obj_has_valid_bookish_data(c):
            skipped += 1
            continue
        if keys & blocked:
            skipped += 1
            continue
        if keys & seen_cycle:
            skipped += 1
            continue
        safe.append(c)
        seen_cycle |= keys
    print("CCC_CANDIDATE_FILTER in=", len(candidates or []), "out=", len(safe), "skipped=", skipped, "blocked_known=", len(blocked))
    return safe


def _ccc_filter_planned_orders(planned):
    blocked = _ccc_load_blocked() | _ccc_portfolio_keys()
    safe = []
    skipped = 0
    seen_cycle = set()
    for o in planned or []:
        keys = _ccc_keys_from_obj(o, include_tokens=True)
        if not keys:
            skipped += 1
            continue
        if not _ccc_obj_has_valid_bookish_data(o):
            skipped += 1
            continue
        if keys & blocked:
            skipped += 1
            continue
        if keys & seen_cycle:
            skipped += 1
            continue
        safe.append(o)
        seen_cycle |= keys
        # Persist after choosing, so next cycle does not hammer the same market/event.
        blocked |= keys
    _ccc_save_blocked(blocked)
    print("CCC_PLANNED_FILTER in=", len(planned or []), "out=", len(safe), "skipped=", skipped, "blocked_total=", len(blocked))
    return safe, skipped
# === CCC HARDENING END ===
'''


def patch_stable(path: Path) -> None:
    s = path.read_text()
    backup(path)
    # Make wrapper defaults overridable by shell/env. This fixes logs ignoring LIVE_MAX_MARKETS etc.
    for key in LIVE_KEYS:
        s = re.sub(
            rf'os\.environ\[["\']{key}["\']\]\s*=\s*([^\n]+)',
            rf'os.environ.setdefault("{key}", \1)',
            s,
        )
    path.write_text(s)


def patch_live(path: Path) -> None:
    s = path.read_text()
    backup(path)
    s = strip_old_hardening(s)

    m = re.search(r"\ndef\s+run_once\s*\(", s)
    if not m:
        raise SystemExit("NO_ENCUENTRO def run_once(...) en live_sports_maker.py")
    s = s[:m.start()] + "\n" + HELPER + "\n" + s[m.start():]

    # Filter candidates immediately after scan_candidates(...) when possible.
    scan_pat = re.search(r"^(\s*)candidates\s*,\s*scan\s*=\s*scan_candidates\([^\n]+\)\s*$", s, flags=re.M)
    if scan_pat:
        indent = scan_pat.group(1)
        insert_at = scan_pat.end()
        s = s[:insert_at] + "\n" + indent + "candidates = _ccc_filter_candidates(candidates)" + s[insert_at:]
    else:
        print("WARN: no encontré línea candidates, scan = scan_candidates(...); queda filtro final de planned")

    # Final defense before submission loop.
    loop_pat = re.search(r"^(\s*)for\s+order\s+in\s+planned\s*:\s*$", s, flags=re.M)
    if not loop_pat:
        raise SystemExit("NO_ENCUENTRO for order in planned:")
    indent = loop_pat.group(1)
    insert = indent + "planned, safe_skipped_repeat_markets = _ccc_filter_planned_orders(planned)\n"
    s = s[:loop_pat.start()] + insert + s[loop_pat.start():]

    path.write_text(s)


def main() -> None:
    project = find_project_dir()
    print("PROJECT:", project)
    os.chdir(project)

    patch_stable(project / "stable_maker_no_cancel.py")
    patch_live(project / "live_sports_maker.py")

    for name in ["live_sports_maker.py", "stable_maker_no_cancel.py", "live_exit_data_api.py", "live_risk_preflight.py"]:
        if (project / name).exists():
            py_compile.compile(str(project / name), doraise=True)
            print("COMPILE_OK", name)

    print("CCC_HARDEN_PATCH_OK")
    print("Next: run dry tests first, then real runner.")


if __name__ == "__main__":
    main()
