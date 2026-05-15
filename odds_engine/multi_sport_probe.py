from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Controlled probe list. This does not enable live trading and does not touch wallet/CLOB.
# The engine still runs in OBSERVE/PAPER only; PaperTrade creation remains gated by risk.
SPORT_KEYS: list[str] = [
    "baseball_mlb",
    "basketball_nba",
    "icehockey_nhl",
    "soccer_epl",
    "soccer_spain_la_liga",
]


def controlled_sport_keys(current: Iterable[str] | None = None) -> list[str]:
    """Return deterministic probe sports, preserving any existing configured sport first."""
    out: list[str] = []
    for key in list(current or []) + SPORT_KEYS:
        key = str(key or "").strip()
        if key and key not in out:
            out.append(key)
    return out


def new_stats() -> dict[str, Any]:
    return {
        "events": 0,
        "odds_outcomes": 0,
        "polymarket_markets": 0,
        "mapping_candidates": 0,
        "auto_approved_mappings": 0,
        "signals": 0,
        "approved_signals": 0,
        "positive_net_edge_signals": 0,
        "paper_trades_created": 0,
        "blocked_reasons": {},
    }


def bump_blocked(stats: dict[str, Any], reason: str | None) -> None:
    reason = str(reason or "unknown")
    blocked = stats.setdefault("blocked_reasons", {})
    blocked[reason] = int(blocked.get(reason, 0)) + 1


def top_blocked_reason(stats: dict[str, Any]) -> str | None:
    blocked = stats.get("blocked_reasons") or {}
    if not blocked:
        return None
    return max(blocked.items(), key=lambda kv: int(kv[1]))[0]


def probe_payload(sport_key: str, stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "sport_key": sport_key,
        "events": int(stats.get("events", 0)),
        "odds_outcomes": int(stats.get("odds_outcomes", 0)),
        "polymarket_markets": int(stats.get("polymarket_markets", 0)),
        "mapping_candidates": int(stats.get("mapping_candidates", 0)),
        "auto_approved_mappings": int(stats.get("auto_approved_mappings", 0)),
        "signals": int(stats.get("signals", 0)),
        "approved_signals": int(stats.get("approved_signals", 0)),
        "positive_net_edge_signals": int(stats.get("positive_net_edge_signals", 0)),
        "paper_trades_created": int(stats.get("paper_trades_created", 0)),
        "top_blocked_reason": top_blocked_reason(stats),
        "blocked_reasons": dict(stats.get("blocked_reasons") or {}),
    }
