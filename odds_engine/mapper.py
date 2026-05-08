from __future__ import annotations

from datetime import datetime, timezone
from rapidfuzz import fuzz
import re
import unicodedata

from config import settings
from models import ExternalEvent, PolymarketMarket, MappingCandidate

ALIASES = {
    'man utd': 'manchester united',
    'man united': 'manchester united',
    'inter milan': 'internazionale',
    'psg': 'paris saint germain',
    'real madrid cf': 'real madrid',
}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(r'\b(fc|cf|club|the|team)\b', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return ALIASES.get(s, s)


def _parse_dt(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _time_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    ev_dt = _parse_dt(event.commence_time_utc)
    pm_dt = _parse_dt(market.start_date) or _parse_dt(market.end_date)
    if not ev_dt or not pm_dt:
        return 0.35
    hours = abs((pm_dt - ev_dt).total_seconds()) / 3600
    if hours <= 3:
        return 1.0
    if hours <= 8:
        return 0.85
    if hours <= 24:
        return 0.55
    return 0.0


def classify_market_type(question: str) -> str:
    q = normalize_name(question)
    if any(x in q for x in ['regular time', 'regulation', 'first half', 'second half', 'spread', 'handicap', 'over ', 'under ', 'total', 'score', 'round', 'method']):
        return 'unsupported_derivative'
    if 'win the world cup' in q or 'win ucl' in q or 'win the champions league' in q or 'champion' in q:
        return 'tournament_winner'
    if 'beat' in q or 'defeat' in q or 'win?' in q or q.startswith('will'):
        return 'moneyline_full_event'
    return 'unknown'


def _participant_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    q = normalize_name(market.question + ' ' + market.slug)
    parts = [normalize_name(event.home_team), normalize_name(event.away_team)]
    parts = [p for p in parts if p]
    if not parts:
        return 0.0
    scores = []
    for p in parts:
        scores.append(max(fuzz.partial_ratio(p, q), fuzz.token_set_ratio(p, q)) / 100.0)
    if len(scores) == 1:
        return scores[0]
    # both participants should appear for match markets; outrights may only include one.
    return sum(scores) / len(scores)


def _sport_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    text = normalize_name(' '.join([event.sport_key, event.league, market.category, market.question]))
    if 'mma' in event.sport_key or 'ufc' in text:
        return 1.0 if any(x in text for x in ['mma', 'ufc', 'fight']) else 0.5
    if 'soccer' in event.sport_key or 'football' in text:
        return 1.0 if any(x in text for x in ['soccer', 'football', 'champions', 'premier', 'liga', 'cup']) else 0.5
    return 0.5


def build_mapping_candidates(events: list[ExternalEvent], markets: list[PolymarketMarket]) -> list[MappingCandidate]:
    candidates: list[MappingCandidate] = []
    for ev in events:
        for pm in markets:
            mtype = classify_market_type(pm.question)
            sport = _sport_score(ev, pm)
            participants = _participant_score(ev, pm)
            timing = _time_score(ev, pm)
            market_type_score = 0.0 if mtype == 'unsupported_derivative' else (0.85 if mtype in {'moneyline_full_event', 'tournament_winner'} else 0.35)
            outcome_clarity = 0.8 if pm.yes_token_id and pm.best_ask is not None else 0.3
            confidence = (0.20 * sport) + (0.35 * participants) + (0.20 * timing) + (0.15 * market_type_score) + (0.10 * outcome_clarity)
            if confidence < 0.50:
                continue
            status = 'auto_approved' if confidence >= settings.min_mapping_confidence else 'needs_review'
            candidates.append(MappingCandidate(
                external_event_id=ev.id,
                polymarket_market_id=pm.id,
                confidence_score=round(confidence, 4),
                status=status,
                market_type=mtype,
                outcome_mapping={'yes_token_id': pm.yes_token_id, 'no_token_id': pm.no_token_id, 'note': 'YES mapping inferred from market title; review required below threshold'},
                confidence_breakdown={'sport': sport, 'participants': participants, 'time': timing, 'market_type': market_type_score, 'outcome_clarity': outcome_clarity, 'question': pm.question},
            ))
    candidates.sort(key=lambda x: x.confidence_score, reverse=True)
    return candidates


def best_candidate_for_event(event_id: str, candidates: list[MappingCandidate]) -> MappingCandidate | None:
    matches = [c for c in candidates if c.external_event_id == event_id]
    if not matches:
        return None
    if len(matches) > 1 and matches[0].confidence_score - matches[1].confidence_score < 0.08:
        matches[0].status = 'needs_review'
    return matches[0]
