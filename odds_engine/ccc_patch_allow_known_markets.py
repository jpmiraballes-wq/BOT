#!/usr/bin/env python3
"""
CCC live maker hotfix.

Problem seen in live logs:
    CCC_CANDIDATE_FILTER in= 0 out= 0 skipped= 0 blocked_known= 411
    orders_planned=0

That means the maker is alive, authenticated, and scanning, but a local hardening
filter is blocking every known/repeated market before order planning.

This patch is intentionally local/runtime-safe:
- backs up each target file once per timestamp
- injects permissive env defaults
- bypasses obvious known/repeat/seen continue-blocks
- keeps all risk caps, spread caps, balance checks and real/dry behavior intact
"""
from __future__ import annotations

import os
import py_compile
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS = [
    ROOT / "live_sports_maker.py",
    ROOT / "stable_maker_no_cancel.py",
]

ENV_INJECT = '''
# CCC AUTO PATCH: allow live maker to reuse known markets.
# The previous hardening layer could block every candidate as blocked_known.
import os as _ccc_allow_known_os
_ccc_allow_known_os.environ.setdefault("LIVE_NO_REPEAT_MARKET", "0")
_ccc_allow_known_os.environ.setdefault("CCC_NO_REPEAT_MARKET", "0")
_ccc_allow_known_os.environ.setdefault("LIVE_ALLOW_REPEAT_MARKET", "1")
_ccc_allow_known_os.environ.setdefault("CCC_ALLOW_REPEAT_MARKET", "1")
_ccc_allow_known_os.environ.setdefault("CCC_ALLOW_KNOWN_MARKETS", "1")
_ccc_allow_known_os.environ.setdefault("CCC_DISABLE_KNOWN_BLOCK", "1")
_ccc_allow_known_os.environ.setdefault("CCC_IGNORE_KNOWN_MARKETS", "1")
_ccc_allow_known_os.environ.setdefault("CCC_BYPASS_KNOWN_FILTER", "1")
'''


def inject_after_imports(src: str) -> str:
    if "CCC AUTO PATCH: allow live maker to reuse known markets" in src:
        return src
    # Preserve shebang and module docstring/imports as much as possible.
    m = re.search(r"((?:#!.*\n)?(?:\"\"\".*?\"\"\"\s*)?(?:from __future__ import .*?\n)?(?:import .*?\n|from .*? import .*?\n)+)", src, re.S)
    if m:
        return src[: m.end()] + ENV_INJECT + src[m.end() :]
    return ENV_INJECT + "\n" + src


def patch_known_continue_blocks(src: str) -> str:
    # Bypass common block style:
    # if <known/repeat/seen/already/...>:
    #     blocked_known += 1
    #     continue
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

    # Bypass helper functions that explicitly return True for known/repeat blocks.
    src = re.sub(
        rf"(def\s+[^\n]*(?:{terms})[^\n]*\([^\n]*\):\s*\n)(?P<body>(?:\s+[^\n]*\n)+?)",
        lambda m: m.group(1) + re.match(r"\s*", m.group("body")).group(0) + "return False  # CCC patched\n" if "return True" in m.group("body") else m.group(0),
        src,
        flags=re.I,
    )
    return src


def patch_env_defaults(src: str) -> str:
    replacements = {
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


def patch_file(path: Path) -> bool:
    if not path.exists():
        print(f"ALLOW_KNOWN_PATCH_SKIP missing={path.name}", flush=True)
        return False
    src = path.read_text()
    original = src
    src = inject_after_imports(src)
    src = patch_env_defaults(src)
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
