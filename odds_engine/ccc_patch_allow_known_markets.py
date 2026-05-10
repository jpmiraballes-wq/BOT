#!/usr/bin/env python3
"""
CCC live maker hotfix — SAFE VERSION.

What this does:
- Keeps the useful fix: known/repeated markets must not block every candidate.
- Restores the old signed maker path: stable_maker_no_cancel.py / live_sports_maker.py.
- Explicitly removes the emergency simple maker from autopilot priority.

Why:
The simple maker was introduced only to bypass the blocked_known=411 situation, but live logs showed
it creates orders with an incompatible signature/order version for the wallet/client setup:
    order_version_mismatch
    invalid signature
The old maker/client path was the one that already bought/sold/cancelled correctly.
"""
from __future__ import annotations

import os
import py_compile
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = [
    ROOT / "ccc_live_autopilot.py",
    ROOT / "live_sports_maker.py",
    ROOT / "stable_maker_no_cancel.py",
]

ENV_INJECT = '''
# CCC AUTO PATCH: allow live maker to reuse known markets.
# The previous hardening layer could block every candidate as blocked_known.
import os as _ccc_allow_known_os
_ccc_allow_known_os.environ["LIVE_NO_REPEAT_MARKET"] = "0"
_ccc_allow_known_os.environ["CCC_NO_REPEAT_MARKET"] = "0"
_ccc_allow_known_os.environ["LIVE_ALLOW_REPEAT_MARKET"] = "1"
_ccc_allow_known_os.environ["CCC_ALLOW_REPEAT_MARKET"] = "1"
_ccc_allow_known_os.environ["CCC_ALLOW_KNOWN_MARKETS"] = "1"
_ccc_allow_known_os.environ["CCC_DISABLE_KNOWN_BLOCK"] = "1"
_ccc_allow_known_os.environ["CCC_IGNORE_KNOWN_MARKETS"] = "1"
_ccc_allow_known_os.environ["CCC_BYPASS_KNOWN_FILTER"] = "1"
'''


def inject_after_imports(src: str) -> str:
    if "CCC AUTO PATCH: allow live maker to reuse known markets" in src:
        return src
    m = re.search(r"((?:#!.*\n)?(?:\"\"\".*?\"\"\"\s*)?(?:from __future__ import .*?\n)?(?:import .*?\n|from .*? import .*?\n)+)", src, re.S)
    if m:
        return src[: m.end()] + ENV_INJECT + src[m.end() :]
    return ENV_INJECT + "\n" + src


def patch_known_continue_blocks(src: str) -> str:
    terms = r"known|repeat|repeated|seen|already|duplicate|dedupe|no_repeat|blocked_known"
    patterns = [
        rf"if\s+[^\n]*(?:{terms})[^\n]*:\s*\n(?P<i>\s+)(?:[^\n]*\n){{0,8}}?\s*blocked_known\s*\+=\s*1\s*\n\s*continue",
        rf"if\s+[^\n]*(?:{terms})[^\n]*:\s*\n(?P<i>\s+)(?:[^\n]*\n){{0,8}}?\s*blocked_total\s*\+=\s*1\s*\n\s*continue",
    ]
    for pat in patterns:
        def repl(m: re.Match[str]) -> str:
            indent = m.group("i")
            return "if False:  # CCC patched: do not block all known/repeated markets\n" + indent + "pass"
        src = re.sub(pat, repl, src, flags=re.I)
    return src


def patch_env_defaults(src: str) -> str:
    replacements = {
        '"LIVE_NO_REPEAT_MARKET": "1"': '"LIVE_NO_REPEAT_MARKET": "0"',
        "'LIVE_NO_REPEAT_MARKET': '1'": "'LIVE_NO_REPEAT_MARKET': '0'",
        '"CCC_NO_REPEAT_MARKET": "1"': '"CCC_NO_REPEAT_MARKET": "0"',
        "'CCC_NO_REPEAT_MARKET': '1'": "'CCC_NO_REPEAT_MARKET': '0'",
        'os.environ.get("LIVE_NO_REPEAT_MARKET", "1")': 'os.environ.get("LIVE_NO_REPEAT_MARKET", "0")',
        "os.environ.get('LIVE_NO_REPEAT_MARKET', '1')": "os.environ.get('LIVE_NO_REPEAT_MARKET', '0')",
        'os.getenv("LIVE_NO_REPEAT_MARKET", "1")': 'os.getenv("LIVE_NO_REPEAT_MARKET", "0")',
        "os.getenv('LIVE_NO_REPEAT_MARKET', '1')": "os.getenv('LIVE_NO_REPEAT_MARKET', '0')",
        'os.environ.get("CCC_NO_REPEAT_MARKET", "1")': 'os.environ.get("CCC_NO_REPEAT_MARKET", "0")',
        "os.environ.get('CCC_NO_REPEAT_MARKET', '1')": "os.environ.get('CCC_NO_REPEAT_MARKET', '0')",
        'os.getenv("CCC_NO_REPEAT_MARKET", "1")': 'os.getenv("CCC_NO_REPEAT_MARKET", "0")',
        "os.getenv('CCC_NO_REPEAT_MARKET', '1')": "os.getenv('CCC_NO_REPEAT_MARKET', '0')",
    }
    for old, new in replacements.items():
        src = src.replace(old, new)
    return src


def restore_autopilot_signed_maker(src: str) -> str:
    # Do not let autopilot prefer ccc_simple_live_maker.py. That path caused order_version_mismatch / invalid signature.
    src = src.replace(
        'for name in ["ccc_simple_live_maker.py", "stable_maker_no_cancel.py", "live_sports_maker.py"]:',
        'for name in ["stable_maker_no_cancel.py", "live_sports_maker.py"]:',
    )
    src = src.replace(
        '    for name in ["ccc_simple_live_maker.py", "stable_maker_no_cancel.py", "live_sports_maker.py"]:',
        '    for name in ["stable_maker_no_cancel.py", "live_sports_maker.py"]:',
    )

    # Remove simple maker from compile list if present. It can remain on disk, but must not be in the live path.
    src = src.replace('    "ccc_simple_live_maker.py",\n', '')
    return src


def patch_file(path: Path) -> bool:
    if not path.exists():
        print(f"ALLOW_KNOWN_PATCH_SKIP missing={path.name}", flush=True)
        return False

    src = path.read_text()
    original = src

    src = patch_env_defaults(src)

    if path.name == "ccc_live_autopilot.py":
        src = restore_autopilot_signed_maker(src)

    if path.name in {"live_sports_maker.py", "stable_maker_no_cancel.py"}:
        src = inject_after_imports(src)
        src = patch_known_continue_blocks(src)

    if src == original:
        print(f"ALLOW_KNOWN_PATCH_NOOP {path.name}", flush=True)
        py_compile.compile(str(path), doraise=True)
        return False

    stamp = int(time.time())
    bak = path.with_suffix(path.suffix + f".bak_allow_known_{stamp}")
    bak.write_text(original)
    path.write_text(src)
    py_compile.compile(str(path), doraise=True)
    print(f"ALLOW_KNOWN_PATCHED {path.name} backup={bak.name}", flush=True)
    return True


def main() -> None:
    os.environ["LIVE_NO_REPEAT_MARKET"] = "0"
    os.environ["CCC_NO_REPEAT_MARKET"] = "0"
    os.environ["LIVE_ALLOW_REPEAT_MARKET"] = "1"
    os.environ["CCC_ALLOW_REPEAT_MARKET"] = "1"
    os.environ["CCC_ALLOW_KNOWN_MARKETS"] = "1"
    os.environ["CCC_DISABLE_KNOWN_BLOCK"] = "1"

    changed = 0
    for target in TARGETS:
        changed += int(patch_file(target))

    print(f"ALLOW_KNOWN_PATCH_DONE changed={changed}", flush=True)


if __name__ == "__main__":
    main()
