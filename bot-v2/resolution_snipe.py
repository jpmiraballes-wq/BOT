"""resolution_snipe.py - Compra YES casi-garantizado tras resolucion externa.

Logica:
  1) Oracle externo detecta que un evento ya se resolvio:
       - Deportes: ESPN scoreboard API (free, sin key) para NFL/NBA/MLB/soccer.
       - Politica / noticias: pull de RSS (Reuters/AP) reutilizando los feeds
         de news_trading. Buscamos keywords de resolucion definitiva
         ("declared winner", "concedes", "official result", etc.)
  2) Matching: crossea titulares/scores con mercados activos por keywords.
  3) Si encontramos un outcome claramente ganador y el mercado sigue abierto
     (active=true, closed=false) y el precio del YES ganador esta entre
     ENTRY_MIN y ENTRY_MAX, compramos a limit.
  4) Salida: cierra en MAX_HOLD_HOURS o cuando price >= TARGET_SELL.

Capital estricto de StrategyCapital['resolution_snipe']. Tamaño por
entrada MUY conservador ($20) porque si fallamos el match, quedamos largos
en un mercado incorrecto.
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
from config import GAMMA_API_URL
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

STRATEGY = "resolution_snipe"
REQUEST_TIMEOUT = 15

# ENTRY RANGE: si YES ya vale >= 0.99, no hay edge. Si vale < 0.90, puede no
# estar realmente resuelto. Sweet spot 0.92-0.98.
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

RESOLUTION_KEYWORDS = [
    r"declared\s+winner", r"concedes?", r"official\s+result",
    r"projected\s+winner", r"defeated", r"wins?\s+election",
    r"final\s+score", r"game\s+over",
]
RESOLUTION_RE = re.compile("|".join(RESOLUTION_KEYWORDS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
# Oracle: ESPN scoreboards
# ---------------------------------------------------------------------------
def _fetch_espn_finals() -> List[Dict[str, str]]:
    """Devuelve lista de {winner, loser, sport, score} para juegos finalizados."""
    finals = []
    for url in ESPN_SCOREBOARDS:
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.debug("ESPN fetch fallo %s: %s", url, exc)
            continue
        events = data.get("events") or []
        for ev in events:
            comps = (ev.get("competitions") or [])
            if not comps:
                continue
            comp = comps[0]
            status = (comp.get("status") or {}).get("type") or {}
            if status.get("state") != "post":
                continue  # no terminado
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


# ---------------------------------------------------------------------------
# Market fetch
# ---------------------------------------------------------------------------
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
    except requests.RequestException as exc:
        logger.error("res_snipe gamma fetch fallo: %s", exc)
        return []


def _match_sport_final(final: Dict[str, str], markets) -> Optional[Tuple[Dict[str, Any], int]]:
    """Busca mercado + indice del outcome ganador."""
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
# Core
# ---------------------------------------------------------------------------
class ResolutionSniper:
    def __init__(self, order_manager, allocator: Optional[CapitalAllocator] = None):
        self.om = order_manager
        self.allocator = allocator or CapitalAllocator()
        self.positions: Dict[str, Dict[str, Any]] = {}
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
        self.positions[market_id] = {
            "token_id": token_id,
            "entry_price": price,
            "size_tokens": size_tokens,
            "size_usdc": POSITION_SIZE_USDC,
            "opened_at": time.time(),
            "question": market.get("question"),
        }
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
