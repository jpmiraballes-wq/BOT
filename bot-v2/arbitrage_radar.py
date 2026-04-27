"""arbitrage_radar.py — Radar Pinnacle vs Polymarket dentro del loop del bot.

Bolt+Opus 2026-04-27 noche. Plan "Overtake Swisstony" MOV 1.

Cada `RADAR_INTERVAL_SECONDS` (default 60s, ajustable), hace:
  1. GET Polymarket gamma /markets (sports activos, ordenados por volumen).
  2. GET The Odds API /sports/<key>/odds (tenis ATP/WTA + EU football).
  3. Matcher fuzzy: si los dos jugadores/equipos del partido aparecen en el
     título Polymarket, comparamos cuotas.
  4. Si edge >= 5% AND vol_24h >= $5k AND minutes_to_game >= 60 → creamos
     CopyTradeProposal en Base44 con status='approved' y tier='radar_pinnacle'.
     El `copy_executor.drain_pending_fills` se encarga de ejecutar.

NO escribe directo en wallet — pasa por Base44 para que respete circuit breakers
(`RadarCircuitBreaker`) y para que JP vea los proposals en /radar-control.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from base44_client import create_record, list_records

logger = logging.getLogger("arbitrage_radar")

# Config
RADAR_INTERVAL_SECONDS = int(os.environ.get("RADAR_INTERVAL_SECONDS", "60"))
MIN_EDGE_PCT = float(os.environ.get("RADAR_MIN_EDGE_PCT", "5.0"))
MAX_EDGE_PCT = float(os.environ.get("RADAR_MAX_EDGE_PCT", "25.0"))
MIN_VOLUME_USDC = float(os.environ.get("RADAR_MIN_VOLUME_USDC", "5000"))
MIN_MINUTES_TO_GAME = float(os.environ.get("RADAR_MIN_MINUTES_TO_GAME", "60"))
MAX_PROPOSALS_PER_RUN = int(os.environ.get("RADAR_MAX_PROPOSALS_PER_RUN", "3"))
TRADE_SIZE_USDC = float(os.environ.get("RADAR_TRADE_SIZE_USDC", "20"))
TP_TARGET = 0.97
SL_MULT = 0.85

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
POLY_GAMMA = "https://gamma-api.polymarket.com/markets"

STATIC_SPORT_KEYS = [
    "soccer_uefa_champs_league",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
]

POLY_BLOCKED_PATTERNS = re.compile(
    r"\b(o/u|over|under|spread|series|playoffs|championship|tournament|win the|to win)\b",
    re.IGNORECASE,
)

_last_run_at: float = 0.0
_seen_alert_keys: set[str] = set()  # cooldown en memoria por proceso


def _normalize(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _event_matches(poly_title: str, pin_home: str, pin_away: str) -> bool:
    p_title = _normalize(poly_title)
    home = _normalize(pin_home)
    away = _normalize(pin_away)
    home_tokens = [t for t in home.split() if len(t) >= 4]
    away_tokens = [t for t in away.split() if len(t) >= 4]
    home_hit = any(t in p_title for t in home_tokens)
    away_hit = any(t in p_title for t in away_tokens)
    return home_hit and away_hit


def _outcome_matches(poly_outcome: str, pin_outcome: str) -> bool:
    a = _normalize(poly_outcome)
    b = _normalize(pin_outcome)
    if a == b:
        return True
    a_tokens = [t for t in a.split() if len(t) >= 4]
    return any(t in b for t in a_tokens)


def _fetch_polymarket_sports() -> List[Dict[str, Any]]:
    try:
        r = requests.get(
            POLY_GAMMA,
            params={
                "active": "true",
                "closed": "false",
                "limit": 200,
                "order": "volume24hr",
                "ascending": "false",
            },
            headers={"User-Agent": "opus-radar-mac/1.0"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        arr = r.json()
        if not isinstance(arr, list):
            return []
        out = []
        for m in arr:
            tags = [str(t).lower() for t in (m.get("tags") or [])]
            cat = str(m.get("category") or "").lower()
            qstr = str(m.get("question") or "")
            keywords = "sport tennis soccer football baseball basketball"
            is_sport = (
                any(any(k in t for k in keywords.split()) for t in tags)
                or any(k in cat for k in keywords.split())
                or bool(re.search(r"vs\.?\s", qstr, re.I))
            )
            if is_sport:
                out.append(m)
        return out
    except Exception as exc:
        logger.warning("fetch_polymarket_sports: %s", exc)
        return []


def _fetch_active_tennis_keys() -> List[str]:
    if not ODDS_API_KEY:
        return []
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/",
            params={"apiKey": ODDS_API_KEY, "all": "true"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        sports = r.json()
        if not isinstance(sports, list):
            return []
        return [
            s["key"]
            for s in sports
            if re.match(r"^tennis_(atp|wta)_", s.get("key", ""), re.I)
            and s.get("active") is True
            and not s.get("has_outrights")
        ]
    except Exception as exc:
        logger.warning("fetch_active_tennis_keys: %s", exc)
        return []


def _fetch_odds_api_events(sport_key: str) -> List[Dict[str, Any]]:
    if not ODDS_API_KEY:
        return []
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/{sport_key}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu,us,uk",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "bookmakers": "pinnacle,betfair_ex_uk,williamhill,bet365,unibet_eu,marathonbet",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return []
        events = r.json()
        return events if isinstance(events, list) else []
    except Exception as exc:
        logger.warning("fetch_odds_api_events %s: %s", sport_key, exc)
        return []


def _extract_best_odds(pin_event: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    out: Dict[str, Optional[Dict[str, Any]]] = {"home": None, "away": None, "draw": None}
    home_n = _normalize(pin_event.get("home_team", ""))
    away_n = _normalize(pin_event.get("away_team", ""))
    for bm in pin_event.get("bookmakers", []):
        market = next((m for m in bm.get("markets", []) if m.get("key") == "h2h"), None)
        if not market:
            continue
        for o in market.get("outcomes", []):
            name = _normalize(o.get("name", ""))
            try:
                price = float(o.get("price", 0))
            except (TypeError, ValueError):
                continue
            if price <= 1:
                continue
            if name == home_n and (not out["home"] or price > out["home"]["price"]):
                out["home"] = {"price": price, "book": bm.get("title", "?")}
            elif name == away_n and (not out["away"] or price > out["away"]["price"]):
                out["away"] = {"price": price, "book": bm.get("title", "?")}
            elif name == "draw" and (not out["draw"] or price > out["draw"]["price"]):
                out["draw"] = {"price": price, "book": bm.get("title", "?")}
    return out


def _fetch_market_end_date(condition_id: str) -> Optional[str]:
    if not condition_id:
        return None
    try:
        r = requests.get(
            POLY_GAMMA,
            params={"condition_ids": condition_id},
            headers={"User-Agent": "opus-radar-mac/1.0"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        arr = r.json()
        m = arr[0] if isinstance(arr, list) and arr else (arr if isinstance(arr, dict) else None)
        if not m:
            return None
        return m.get("endDate") or m.get("end_date_iso")
    except Exception:
        return None


def _create_proposal(opp: Dict[str, Any]) -> Optional[str]:
    """Crea un CopyTradeProposal en Base44 con status='approved'.

    El copy_executor lo drena en el siguiente loop (5s).
    """
    poly_price = opp["poly_price"]
    sl_pct = SL_MULT - 1
    tp_pct = (TP_TARGET / poly_price) - 1 if 0 < poly_price < TP_TARGET else 0.05

    payload = {
        "status": "approved",
        "quality_score": min(100, round(opp["edge_pct"] * 4)),
        "whale_count": 0,
        "whale_names": ["radar_pinnacle_mac"],
        "whale_addresses": [],
        "whales_detail": [],
        "market_end_date": opp.get("commence_time"),
        "avg_whale_wr": 0,
        "total_whale_usdc": 0,
        "market_question": opp.get("market", ""),
        "market_slug": opp.get("slug", ""),
        "condition_id": opp.get("condition_id", ""),
        "token_id": opp.get("condition_id", ""),
        "outcome": opp.get("outcome", ""),
        "side": "BUY",
        "entry_price": poly_price,
        "suggested_size_usdc": TRADE_SIZE_USDC,
        "amount_usdc": TRADE_SIZE_USDC,
        "take_profit_pct": tp_pct,
        "stop_loss_pct": sl_pct,
        "category": "sports",
        "tier": "radar_pinnacle",
        "rejection_reason": (
            f"mac_radar_edge_{opp['edge_pct']:.1f}pct_{int(opp.get('minutes_to_end', 0))}min"
        ),
    }
    res = create_record("CopyTradeProposal", payload)
    return res.get("id") if isinstance(res, dict) else None


def run_radar_once() -> Dict[str, Any]:
    """Ejecuta una corrida completa del radar. Llamado desde main.py."""
    started = time.time()

    poly_markets = _fetch_polymarket_sports()
    if not poly_markets:
        return {"ok": False, "reason": "no_poly_markets", "duration_s": 0}

    sport_keys = _fetch_active_tennis_keys() + STATIC_SPORT_KEYS
    all_events: List[Dict[str, Any]] = []
    for k in sport_keys:
        for ev in _fetch_odds_api_events(k):
            ev["sport"] = k
            all_events.append(ev)

    candidates: List[Dict[str, Any]] = []
    for m in poly_markets:
        poly_title = m.get("question") or ""
        if POLY_BLOCKED_PATTERNS.search(poly_title):
            continue
        try:
            vol_24h = float(m.get("volume24hr") or m.get("volume24h") or 0)
        except (TypeError, ValueError):
            vol_24h = 0
        if vol_24h < MIN_VOLUME_USDC:
            continue

        matching_ext = [
            e for e in all_events
            if _event_matches(poly_title, e.get("home_team", ""), e.get("away_team", ""))
        ]
        if not matching_ext:
            continue
        ext = matching_ext[0]
        ext_odds = _extract_best_odds(ext)

        try:
            import json as _json
            names = _json.loads(m.get("outcomes", "[]"))
            prices = _json.loads(m.get("outcomePrices", "[]"))
            poly_outcomes = [
                {"name": n, "price": float(prices[i])}
                for i, n in enumerate(names)
                if i < len(prices)
            ]
        except Exception:
            continue

        for po in poly_outcomes:
            p = po.get("price", 0)
            if not (0 < p < 1):
                continue
            ext_match = None
            if _outcome_matches(po["name"], ext.get("home_team", "")):
                ext_match = ext_odds.get("home")
            elif _outcome_matches(po["name"], ext.get("away_team", "")):
                ext_match = ext_odds.get("away")
            elif re.search(r"draw|tie|empate", po["name"], re.I):
                ext_match = ext_odds.get("draw")
            if not ext_match:
                continue

            poly_prob = p
            ext_prob = 1 / ext_match["price"]
            edge_pct = ((ext_prob - poly_prob) / poly_prob) * 100
            if edge_pct < MIN_EDGE_PCT or edge_pct > MAX_EDGE_PCT:
                continue

            alert_key = f"{m.get('conditionId')}::{_normalize(po['name'])}"
            if alert_key in _seen_alert_keys:
                continue

            candidates.append({
                "alert_key": alert_key,
                "market": poly_title,
                "slug": m.get("slug"),
                "condition_id": m.get("conditionId"),
                "outcome": po["name"],
                "poly_price": poly_prob,
                "poly_volume_24h": vol_24h,
                "ext_decimal_odds": ext_match["price"],
                "ext_book": ext_match["book"],
                "ext_implied_prob": ext_prob,
                "edge_pct": edge_pct,
                "sport": ext.get("sport"),
                "commence_time": ext.get("commence_time"),
            })

    # Filtro pre-game >= 60min
    final: List[Dict[str, Any]] = []
    for c in candidates:
        end_iso = _fetch_market_end_date(c["condition_id"])
        if not end_iso:
            continue
        try:
            from datetime import datetime, timezone
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            mins = (end_dt - datetime.now(timezone.utc)).total_seconds() / 60
        except Exception:
            continue
        if mins < MIN_MINUTES_TO_GAME:
            continue
        c["minutes_to_end"] = mins
        final.append(c)

    final.sort(key=lambda x: x["edge_pct"], reverse=True)
    final = final[:MAX_PROPOSALS_PER_RUN]

    created_ids = []
    for c in final:
        pid = _create_proposal(c)
        if pid:
            created_ids.append(pid)
            _seen_alert_keys.add(c["alert_key"])

    duration = time.time() - started
    if final:
        logger.info(
            "radar: poly=%d ext=%d edges=%d created=%d (%.1fs)",
            len(poly_markets), len(all_events), len(candidates), len(created_ids), duration,
        )
    return {
        "ok": True,
        "poly_markets": len(poly_markets),
        "ext_events": len(all_events),
        "edges_total": len(candidates),
        "proposals_created": len(created_ids),
        "duration_s": round(duration, 2),
    }


def maybe_run_radar() -> Optional[Dict[str, Any]]:
    """Llamado en cada loop del bot. Solo ejecuta si pasaron RADAR_INTERVAL_SECONDS."""
    global _last_run_at
    now = time.time()
    if now - _last_run_at < RADAR_INTERVAL_SECONDS:
        return None
    _last_run_at = now

    if not ODDS_API_KEY:
        logger.warning("ODDS_API_KEY vacío; radar inactivo.")
        return None

    try:
        return run_radar_once()
    except Exception as exc:
        logger.error("arbitrage_radar fallo: %s", exc, exc_info=True)
        return None
