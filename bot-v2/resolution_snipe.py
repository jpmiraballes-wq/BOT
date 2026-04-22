"""resolution_snipe.py - Resolution snipe con persistencia Base44.

Compra YES casi-garantizado tras resolucion externa (ESPN / noticias).
Las posiciones abiertas se cargan desde entity Position al arrancar.
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from capital_allocator import CapitalAllocator
from paper_lab import emit_paper_trade  # PAPER_LAB_PATCHED
from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL, GAMMA_API_URL
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

STRATEGY = "resolution_snipe"
REQUEST_TIMEOUT = 15
ENTRY_MIN = 0.92
ENTRY_MAX = 0.98
TARGET_SELL = 0.995
POSITION_SIZE_USDC = 20.0
MAX_CONCURRENT_POSITIONS = 3
MAX_HOLD_HOURS = 2
MIN_VOLUME_USDC = 5_000.0

ESPN_SCOREBOARDS = [
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard",
]


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def _extract_outcome_prices(market) -> List[float]:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [_safe_float(p) for p in raw]


def _extract_outcomes(market) -> List[str]:
    raw = market.get("outcomes")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw]


def _extract_tokens(market) -> List[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


# ---------------------------------------------------------------------------
# Persistencia Base44 - entity Position
# ---------------------------------------------------------------------------
def _b44_headers():
    return {"api_key": BASE44_API_KEY or "", "Content-Type": "application/json"}


def _b44_url(record_id: Optional[str] = None) -> str:
    base = "%s/api/apps/%s/entities/Position" % (BASE44_BASE_URL, BASE44_APP_ID)
    return "%s/%s" % (base, record_id) if record_id else base


def _load_open_positions(strategy: str) -> Dict[str, Dict[str, Any]]:
    if not BASE44_API_KEY:
        return {}
    try:
        resp = requests.get(
            _b44_url(),
            params={"status": "open", "strategy": strategy},
            headers=_b44_headers(), timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return {}
        data = resp.json()
        records = data["data"] if isinstance(data, dict) and "data" in data else data
        if not isinstance(records, list):
            return {}
        positions = {}
        for r in records:
            market_id = r.get("market")
            if not market_id:
                continue
            positions[market_id] = {
                "_record_id": r.get("id"),
                "token_id": r.get("token_id") or "",
                "entry_price": _safe_float(r.get("entry_price")),
                "size_tokens": _safe_float(r.get("size_tokens")),
                "size_usdc": _safe_float(r.get("size_usdc")),
                "opened_at": _safe_float(r.get("opened_at_ts"), time.time()),
                "question": r.get("question") or "",
            }
        logger.info("res_snipe: %d posiciones open recuperadas de Base44", len(positions))
        return positions
    except requests.RequestException as exc:
        logger.error("Position load fallo: %s", exc)
        return {}


def _find_existing_open(strategy: str, token_id: str) -> Optional[Dict[str, Any]]:
    if not BASE44_API_KEY or not token_id:
        return None
    try:
        resp = requests.get(
            _b44_url(),
            params={"status": "open", "strategy": strategy, "token_id": token_id},
            headers=_b44_headers(), timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        records = data["data"] if isinstance(data, dict) and "data" in data else data
        if not isinstance(records, list) or not records:
            return None
        records.sort(key=lambda r: r.get("created_date") or "")
        return records[0]
    except requests.RequestException:
        return None


def _persist_position_open(strategy: str, market_id: str, pos: Dict[str, Any]) -> Optional[str]:
    """Upsert: mergea si ya existe open con mismo token_id+strategy."""
    if not BASE44_API_KEY:
        return None
    token_id = pos.get("token_id") or ""
    existing = _find_existing_open(strategy, token_id) if token_id else None

    if existing:
        old_size_tokens = _safe_float(existing.get("size_tokens"))
        old_size_usdc = _safe_float(existing.get("size_usdc"))
        old_entry = _safe_float(existing.get("entry_price"))
        new_size_tokens = old_size_tokens + pos["size_tokens"]
        new_size_usdc = old_size_usdc + pos["size_usdc"]
        if new_size_tokens > 0:
            new_entry = ((old_entry * old_size_tokens) +
                         (pos["entry_price"] * pos["size_tokens"])) / new_size_tokens
        else:
            new_entry = pos["entry_price"]
        record_id = existing.get("id")
        patch = {
            "entry_price": round(new_entry, 4),
            "size_tokens": round(new_size_tokens, 4),
            "size_usdc": round(new_size_usdc, 4),
        }
        try:
            requests.patch(_b44_url(record_id), json=patch,
                           headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
            logger.info("Position merge token=%s entry=%.4f size_tokens=%.2f",
                        token_id[:8], new_entry, new_size_tokens)
        except requests.RequestException:
            pass
        return record_id

    payload = {
        "market": market_id,
        "side": "BUY",
        "entry_price": pos["entry_price"],
        "current_price": pos["entry_price"],
        "size_usdc": pos["size_usdc"],
        "pnl_unrealized": 0.0,
        "status": "open",
        "strategy": strategy,
        "token_id": pos["token_id"],
        "size_tokens": pos["size_tokens"],
        "opened_at_ts": pos["opened_at"],
        "question": pos.get("question"),
    }
    try:
        resp = requests.post(_b44_url(), json=payload,
                             headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data.get("id") or (data.get("data") or {}).get("id")
    except requests.RequestException:
        return None


def _persist_position_close(record_id: Optional[str], exit_price: float) -> None:
    if not record_id or not BASE44_API_KEY:
        return
    try:
        requests.patch(
            _b44_url(record_id),
            json={"status": "closed", "current_price": exit_price},
            headers=_b44_headers(), timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# Oracle ESPN
# ---------------------------------------------------------------------------
def _fetch_espn_finals() -> List[Dict[str, str]]:
    finals = []
    for url in ESPN_SCOREBOARDS:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            continue
        events = data.get("events") or []
        for ev in events:
            comps = (ev.get("competitions") or [])
            if not comps:
                continue
            comp = comps[0]
            status = (comp.get("status") or {}).get("type") or {}
            if status.get("state") != "post":
                continue
            competitors = comp.get("competitors") or []
            if len(competitors) != 2:
                continue
            winner = None
            loser = None
            for c in competitors:
                name = (c.get("team") or {}).get("displayName") or ""
                if c.get("winner"):
                    winner = name
                else:
                    loser = name
            if winner and loser:
                finals.append({
                    "winner": winner, "loser": loser,
                    "sport": url.rsplit("/", 2)[-2],
                })
    return finals


def _fetch_active_markets() -> List[Dict[str, Any]]:
    url = "%s/markets" % GAMMA_API_URL
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": 300, "order": "volume", "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        markets = data["data"] if isinstance(data, dict) and "data" in data else data
        return markets if isinstance(markets, list) else []
    except requests.RequestException:
        return []


def _match_sport_final(final: Dict[str, str], markets) -> Optional[Tuple[Dict[str, Any], int]]:
    winner_norm = _normalize(final["winner"])
    loser_norm = _normalize(final["loser"])
    if not winner_norm:
        return None
    for m in markets:
        vol = _safe_float(m.get("volume") or m.get("volumeNum"))
        if vol < MIN_VOLUME_USDC:
            continue
        question_norm = _normalize(m.get("question") or "")
        if winner_norm not in question_norm and loser_norm not in question_norm:
            continue
        outcomes = _extract_outcomes(m)
        if not outcomes:
            continue
        winner_idx = None
        for i, o in enumerate(outcomes):
            if winner_norm in _normalize(o):
                winner_idx = i
                break
        if winner_idx is None:
            continue
        return m, winner_idx
    return None


# ---------------------------------------------------------------------------
# ResolutionSniper
# ---------------------------------------------------------------------------
class ResolutionSniper:
    def __init__(self, order_manager, allocator: Optional[CapitalAllocator] = None):
        self.om = order_manager
        self.allocator = allocator or CapitalAllocator()
        # Cargar desde Base44
        self.positions: Dict[str, Dict[str, Any]] = _load_open_positions(STRATEGY)
        self.seen_finals: set = set()

    def _final_key(self, final: Dict[str, str]) -> str:
        return hashlib.sha1(
            ("%s|%s|%s" % (final["winner"], final["loser"],
                           final.get("sport", ""))).encode()
        ).hexdigest()[:12]

    def _maybe_open(self, market, winner_idx: int, budget: float):
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            return
        if budget < POSITION_SIZE_USDC:
            return
        market_id = market.get("id") or market.get("conditionId")
        if not market_id or market_id in self.positions:
            return
        prices = _extract_outcome_prices(market)
        tokens = _extract_tokens(market)
        if winner_idx >= len(prices) or winner_idx >= len(tokens):
            return
        price = prices[winner_idx]
        if not (ENTRY_MIN <= price <= ENTRY_MAX):
            return
        token_id = tokens[winner_idx]
        size_tokens = round(POSITION_SIZE_USDC / price, 2)
        if size_tokens < 5:
            return
        # --- Paper mode branch (PAPER_LAB_PATCHED) ---
        if self.allocator.get_execution_mode(STRATEGY) == "paper":
            emit_paper_trade(
                strategy=STRATEGY,
                market=(market.get("question") or market_id)[:300],
                side="BUY",
                entry_price=price,
                size_usdc=POSITION_SIZE_USDC,
                token_id=token_id,
                condition_id=market_id,
                tp_pct=(TARGET_SELL - price) / price,
                sl_pct=-0.10,
                max_hold_hours=float(MAX_HOLD_HOURS),
                features={
                    "entry_price": price,
                    "target_sell": TARGET_SELL,
                    "winner_idx": winner_idx,
                },
            )
            return
        # --- End paper branch ---
        try:
            order_ids = self.om.place_limit_buy(
                token_id=token_id,
                price=min(0.99, round(price + 0.005, 2)),
                size=size_tokens,
                market_id=market_id,
                strategy=STRATEGY,
            )
        except Exception as exc:
            logger.error("res_snipe place_limit_buy fallo: %s", exc)
            return
        if not order_ids:
            return
        pos = {
            "token_id": token_id,
            "entry_price": price,
            "size_tokens": size_tokens,
            "size_usdc": POSITION_SIZE_USDC,
            "opened_at": time.time(),
            "question": market.get("question"),
        }
        pos["_record_id"] = _persist_position_open(STRATEGY, market_id, pos)
        self.positions[market_id] = pos
        log_decision(reason="res_snipe_entry",
                     market=market.get("question"), strategy=STRATEGY,
                     edge=TARGET_SELL - price, size=POSITION_SIZE_USDC,
                     extra={"entry_price": price})

    def _check_exits(self, markets_by_id: Dict[str, Dict[str, Any]]):
        now = time.time()
        for market_id, pos in list(self.positions.items()):
            age_h = (now - pos["opened_at"]) / 3600.0
            market = markets_by_id.get(market_id)
            cur_price = None
            if market:
                prices = _extract_outcome_prices(market)
                tokens = _extract_tokens(market)
                for i, t in enumerate(tokens):
                    if t == pos["token_id"] and i < len(prices):
                        cur_price = prices[i]
                        break
            exit_reason = None
            if age_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif cur_price is not None and cur_price >= TARGET_SELL:
                exit_reason = "target_hit"
            if not exit_reason:
                continue
            try:
                self.om.close_position_market(
                    token_id=pos["token_id"],
                    size=pos["size_tokens"],
                    market_id=market_id,
                    strategy=STRATEGY,
                )
            except Exception as exc:
                logger.error("res_snipe close_position fallo: %s", exc)
                continue
            _persist_position_close(pos.get("_record_id"),
                                    float(cur_price or pos["entry_price"]))
            log_decision(reason="res_snipe_exit_%s" % exit_reason,
                         market=pos.get("question"), strategy=STRATEGY,
                         extra={"age_h": round(age_h, 2),
                                "cur_price": cur_price})
            self.positions.pop(market_id, None)

    def run_cycle(self):
        if not self.allocator.is_enabled(STRATEGY):
            return
        markets = _fetch_active_markets()
        if not markets:
            return
        markets_by_id = {
            (m.get("id") or m.get("conditionId")): m for m in markets if m
        }
        self._check_exits(markets_by_id)

        budget = self.allocator.get_available(STRATEGY)
        if budget < POSITION_SIZE_USDC:
            self.allocator.report_deployed(
                STRATEGY,
                sum(p["size_usdc"] for p in self.positions.values()),
            )
            return

        finals = _fetch_espn_finals()
        for final in finals:
            key = self._final_key(final)
            if key in self.seen_finals:
                continue
            matched = _match_sport_final(final, markets)
            if not matched:
                continue
            market, winner_idx = matched
            self._maybe_open(market, winner_idx, budget)
            self.seen_finals.add(key)
            budget = self.allocator.get_available(STRATEGY) - \
                sum(p["size_usdc"] for p in self.positions.values())
            if budget < POSITION_SIZE_USDC:
                break

        deployed = sum(p["size_usdc"] for p in self.positions.values())
        self.allocator.report_deployed(STRATEGY, deployed)
