#!/usr/bin/env python3
"""Patch ccc_live_autopilot.py to prefer ccc_simple_live_maker.py.

This is intentionally tiny and idempotent. It only changes maker selection and
compile targets. It does not touch exits, cancels, preflight, or credentials.
"""
from __future__ import annotations

from pathlib import Path
import py_compile
import time

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "ccc_live_autopilot.py"


def main() -> int:
    if not TARGET.exists():
        print(f"SIMPLE_MAKER_PATCH_SKIP missing={TARGET.name}", flush=True)
        return 0

    s = TARGET.read_text()
    original = s

    if '"ccc_simple_live_maker.py"' not in s:
        s = s.replace(
            '    "stable_maker_no_cancel.py",\n    "live_sports_maker.py",',
            '    "ccc_simple_live_maker.py",\n    "stable_maker_no_cancel.py",\n    "live_sports_maker.py",',
        )

    old = '    for name in ["stable_maker_no_cancel.py", "live_sports_maker.py"]:'
    new = '    for name in ["ccc_simple_live_maker.py", "stable_maker_no_cancel.py", "live_sports_maker.py"]:'
    if old in s:
        s = s.replace(old, new)

    if s != original:
        backup = TARGET.with_suffix(TARGET.suffix + f".bak_simple_maker_{int(time.time())}")
        backup.write_text(original)
        TARGET.write_text(s)
        print(f"SIMPLE_MAKER_PATCHED {TARGET.name} backup={backup.name}", flush=True)
    else:
        print(f"SIMPLE_MAKER_PATCH_OK already_preferred {TARGET.name}", flush=True)

    py_compile.compile(str(TARGET), doraise=True)
    if (ROOT / "ccc_simple_live_maker.py").exists():
        py_compile.compile(str(ROOT / "ccc_simple_live_maker.py"), doraise=True)
    print("SIMPLE_MAKER_PATCH_DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
