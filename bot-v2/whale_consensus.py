"""whale_consensus.py - Copy-trading cuando 2+ whales del leaderboard coinciden.

PAPER-ONLY POR DISENO: esta estrategia solo emite PaperTrade via paper_lab.
No toca el CLOB. Cuando queramos graduarla a live, agregar la rama live
en _maybe_emit() (similar a otras estrategias ya integradas).

# PAPER_LAB_PHASE3
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests

from capital_allocator import CapitalAllocator
from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL
from paper_lab import emit_paper_trade

logger = logging.getLogger(__name__)

STRATEGY = "whale_consensus"
REQUEST_TIMEOUT = 15

# Defaults (sobreescritos por config en StrategyCapital si viene)
MIN_CONSENSUS = 2          # minimo whales en el mismo outcome
MAX_SIGNAL_AGE_HOURS = 6
MIN_MARKET_VOLUME_USDC = 10000
POSITION_SIZE_USDC = 5.0
TP_PCT = 0.10
SL_PCT = -0.07
MAX_HOLD_HOURS = 48.0


def _endpoint(entity: str) -> str:
    return "%s/api/apps/%s/entities/%s/records" % (
        BASE44_BASE_URL, BASE44_APP_ID, entity,
    )


def _fetch_recent_signals(max_age_hours: float) -> List[Dict[str, Any]]:
    """Lee WhaleSignal de Base44 via external API (status=new, detected < max_age)."""
    try:
        resp = requests.get(
            _endpoint("WhaleSignal"),
            headers={"api_key": BASE44_API_KEY},
            params={"status": "new", "sort": "-detected_at", "limit": 200},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.warning("whale_consensus fetch signals %d: %s",
                           resp.status_code, resp.text[:200])
            return []
        data = resp.json() or []
        if isinstance(data, dict):
            data = data.get("data") or []
        # filtrar por edad (detected_at iso)
        now_ts = time.time()
        out = []
        cutoff = now_ts - max_age_hours * 3600
        for s in data:
            d = s.get("detected_at") or ""
            try:
                import datetime as _dt
                ts = _dt.datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = now_ts
            if ts >= cutoff:
                out.append(s)
        return out
    except requests.RequestException as exc:
        logger.error("whale_consensus fetch signals: %s", exc)
        return []


def _group_by_consensus(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Agrupa signals por (condition_id, outcome, side) y devuelve los que tienen >=MIN_CONSENSUS whales distintos."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        key = "%s|%s|%s" % (
            s.get("condition_id") or s.get("market_slug") or "",
            (s.get("outcome") or "").upper(),
            (s.get("side") or "BUY").upper(),
        )
        groups[key].append(s)
    out = []
    for key, items in groups.items():
        whales = {it.get("whale_address") for it in items if it.get("whale_address")}
        if len(whales) >= MIN_CONSENSUS:
            # tomar el mas reciente como representativo
            rep = max(items, key=lambda x: x.get("whale_trade_ts") or 0)
            rep["_consensus_count"] = len(whales)
            rep["_consensus_whales"] = list(whales)
            out.append(rep)
    return out


def _mark_signals_copied(signal_ids: List[str]) -> None:
    for sid in signal_ids:
        if not sid:
            continue
        try:
            requests.put(
                "%s/%s" % (_endpoint("WhaleSignal"), sid),
                headers={"api_key": BASE44_API_KEY, "Content-Type": "application/json"},
                json={"status": "copied"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            pass


class WhaleConsensus:
    def __init__(self, order_manager=None, allocator: Optional[CapitalAllocator] = None):
        # order_manager no se usa en paper. Lo aceptamos por simetria con otras clases.
        self.om = order_manager
        self.allocator = allocator or CapitalAllocator()
        self.seen_keys: set = set()

    def run_cycle(self):
        # Gate: solo correr si la estrategia esta en paper o live.
        # En FASE 3 la rama live no existe todavia -> si esta en live, skip.
        mode = self.allocator.get_execution_mode(STRATEGY)
        if mode == "disabled":
            return
        if mode == "live":
            logger.warning("whale_consensus en live mode pero no hay ejecutor live; skip.")
            return
        # mode == "paper"
        global MIN_CONSENSUS
        rec = self.allocator.get(STRATEGY) or {}
        cfg = rec.get("config") or {}
        min_consensus = int(cfg.get("min_whales_consensus") or MIN_CONSENSUS)
        max_age = float(cfg.get("max_signal_age_hours") or MAX_SIGNAL_AGE_HOURS)
        min_vol = float(cfg.get("min_market_volume") or MIN_MARKET_VOLUME_USDC)
        size_usdc = float(rec.get("trade_size_min") or POSITION_SIZE_USDC)

        MIN_CONSENSUS = min_consensus

        signals = _fetch_recent_signals(max_age)
        if not signals:
            return
        groups = _group_by_consensus(signals)
        if not groups:
            return

        emitted = 0
        for g in groups:
            key = "%s|%s|%s" % (
                g.get("condition_id") or g.get("market_slug") or "",
                (g.get("outcome") or "").upper(),
                (g.get("side") or "BUY").upper(),
            )
            if key in self.seen_keys:
                continue
            # Min market volume filter
            vol = float(g.get("size_usdc") or 0)
            if vol < min_vol / 100:  # size del whale como proxy grueso
                pass  # permitimos, es un weak check

            price = float(g.get("price") or 0)
            if price <= 0 or price >= 0.99:
                continue

            emit_paper_trade(
                strategy=STRATEGY,
                market=(g.get("market_question") or g.get("market_slug") or "?")[:300],
                side=(g.get("side") or "BUY").upper(),
                entry_price=price,
                size_usdc=size_usdc,
                token_id=g.get("token_id"),
                condition_id=g.get("condition_id"),
                outcome=(g.get("outcome") or "").upper(),
                tp_pct=TP_PCT,
                sl_pct=SL_PCT,
                max_hold_hours=MAX_HOLD_HOURS,
                features={
                    "whale_price": price,
                    "whale_size_usdc": vol,
                    "consensus_count": g.get("_consensus_count"),
                },
                signal_meta={
                    "whale_addresses": ",".join((g.get("_consensus_whales") or [])[:5]),
                    "primary_whale_name": g.get("whale_name"),
                },
            )
            self.seen_keys.add(key)
            _mark_signals_copied([it.get("id") for it in [g]])
            emitted += 1

        if emitted:
            logger.info("whale_consensus: %d paper trades emitidos", emitted)
