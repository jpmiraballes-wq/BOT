from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional
import hashlib
import json


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(*parts: Any) -> str:
    raw = '|'.join(str(p or '') for p in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def to_dict(obj: Any) -> dict:
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    return {'value': obj}


@dataclass
class ExternalEvent:
    id: str
    provider: str
    provider_event_id: str
    sport_key: str
    league: str
    home_team: str
    away_team: str
    commence_time_utc: str
    participants_normalized: list[str]
    raw_payload: dict


@dataclass
class OddsOutcome:
    external_event_id: str
    bookmaker: str
    market_key: str
    outcome_name: str
    decimal_odds: float
    implied_probability_raw: float
    implied_probability_normalized: float
    provider_last_update: Optional[str]
    received_at: str
    raw_payload: dict


@dataclass
class PolymarketMarket:
    id: str
    question: str
    slug: str
    category: str
    start_date: Optional[str]
    end_date: Optional[str]
    condition_id: Optional[str]
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    outcomes: list[str]
    best_bid: Optional[float]
    best_ask: Optional[float]
    midpoint: Optional[float]
    spread: Optional[float]
    liquidity: float
    raw_payload: dict


@dataclass
class MappingCandidate:
    external_event_id: str
    polymarket_market_id: str
    confidence_score: float
    status: str
    market_type: str
    outcome_mapping: dict
    confidence_breakdown: dict


@dataclass
class Signal:
    id: str
    strategy: str
    external_event_id: str
    polymarket_market_id: str
    token_id: str
    action: str
    fair_value: float
    polymarket_price: float
    edge_bruto: float
    edge_neto: float
    spread: float
    liquidity: float
    confidence: float
    freshness_status: str
    mapping_status: str
    risk_status: str
    reject_reason: str
    mode: str
    explanation: str
    created_at: str


@dataclass
class PaperTrade:
    id: str
    signal_id: str
    external_event_id: str
    polymarket_market_id: str
    token_id: str
    side: str
    entry_price: float
    exit_price: Optional[float]
    size_usd: float
    quantity: float
    status: str
    pnl_usd: float
    pnl_pct: float
    opened_at: str
    closed_at: Optional[str]
    reason_open: str
    reason_close: Optional[str]


@dataclass
class BotLog:
    level: str
    source: str
    message: str
    data: dict
    created_at: str
