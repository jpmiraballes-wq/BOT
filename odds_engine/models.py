from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional
import hashlib


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

    def base44_payload(self) -> dict:
        return {
            'source': self.provider,
            'external_id': self.provider_event_id,
            'sport': self.sport_key,
            'league': self.league,
            'home': self.home_team,
            'away': self.away_team,
            'title': f'{self.home_team} vs {self.away_team}'.strip(' vs '),
            'starts_at': self.commence_time_utc,
            'raw': self.raw_payload,
        }


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

    def base44_payload(self) -> dict:
        return {
            'external_event_id': self.external_event_id,
            'outcome': self.outcome_name,
            'decimal_odds': self.decimal_odds,
            'implied_probability': self.implied_probability_normalized,
            'captured_at': self.provider_last_update or self.received_at,
        }


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

    def base44_event_payload(self) -> dict:
        return {
            'polymarket_id': self.raw_payload.get('eventId') or self.raw_payload.get('event_id') or self.id,
            'slug': self.slug,
            'title': self.raw_payload.get('eventTitle') or self.question,
            'category': self.category,
            'ends_at': self.end_date,
            'raw': self.raw_payload,
        }

    def base44_market_payload(self) -> dict:
        return {
            'polymarket_event_id': self.raw_payload.get('eventId') or self.raw_payload.get('event_id') or self.id,
            'condition_id': self.condition_id or self.id,
            'question': self.question,
            'outcomes': self.outcomes,
            'token_ids': [x for x in [self.yes_token_id, self.no_token_id] if x],
            'ends_at': self.end_date,
            'active': True,
            'raw': self.raw_payload,
        }

    def base44_snapshot_payload(self) -> dict:
        price = self.best_ask if self.best_ask is not None else self.midpoint
        return {
            'polymarket_market_id': self.id,
            'outcome': self.outcomes[0] if self.outcomes else 'YES',
            'price': price,
            'best_bid': self.best_bid,
            'best_ask': self.best_ask,
            'spread_pct': self.spread,
            'liquidity_usdc': self.liquidity,
            'captured_at': now_iso(),
        }


@dataclass
class MappingCandidate:
    external_event_id: str
    polymarket_market_id: str
    confidence_score: float
    status: str
    market_type: str
    outcome_mapping: dict
    confidence_breakdown: dict

    def base44_event_mapping_payload(self) -> dict:
        return {
            'external_event_id': self.external_event_id,
            'polymarket_event_id': self.polymarket_market_id,
            'confidence': self.confidence_score,
            'method': 'auto_fuzzy',
            'verified': self.status == 'auto_approved',
        }

    def base44_market_mapping_payload(self) -> dict:
        return {
            'event_mapping_id': stable_id('event_mapping', self.external_event_id, self.polymarket_market_id),
            'polymarket_market_id': self.polymarket_market_id,
            'external_outcome': str(self.outcome_mapping.get('external_outcome') or ''),
            'polymarket_outcome': str(self.outcome_mapping.get('polymarket_outcome') or 'YES'),
            'verified': self.status == 'auto_approved',
        }


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

    def base44_payload(self) -> dict:
        return {
            'event_mapping_id': stable_id('event_mapping', self.external_event_id, self.polymarket_market_id),
            'market_mapping_id': stable_id('market_mapping', self.external_event_id, self.polymarket_market_id, self.token_id),
            'external_event_id': self.external_event_id,
            'polymarket_market_id': self.polymarket_market_id,
            'outcome': 'YES',
            'fair_probability': self.fair_value,
            'polymarket_price': self.polymarket_price,
            'edge_pct': self.edge_bruto,
            'edge_neto': self.edge_neto,
            'spread_pct': self.spread,
            'liquidity_usdc': self.liquidity,
            'mapping_confidence': self.confidence,
            'mapping_status': self.mapping_status,
            'freshness_status': self.freshness_status,
            'risk_status': self.risk_status,
            'reject_reason': self.reject_reason,
            'explanation': self.explanation,
            'mode': self.mode,
            'side': 'BUY',
            'status': 'pending' if self.risk_status == 'approved' else 'rejected',
            'detected_at': self.created_at,
        }


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

    def base44_payload(self) -> dict:
        return {
            'signal_id': self.signal_id,
            'polymarket_market_id': self.polymarket_market_id,
            'outcome': 'YES',
            'side': 'BUY' if self.side.upper() in {'YES', 'BUY'} else 'SELL',
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'size_usdc': self.size_usd,
            'pnl_realized': self.pnl_usd,
            'pnl_pct': self.pnl_pct,
            'status': self.status,
            'opened_at': self.opened_at,
            'closed_at': self.closed_at,
            'close_reason': self.reason_close,
        }


@dataclass
class BotLog:
    level: str
    source: str
    message: str
    data: dict
    created_at: str

    def base44_payload(self) -> dict:
        return {
            'level': 'warn' if self.level == 'warning' else self.level,
            'module': self.source,
            'message': self.message,
            'data': self.data,
        }
