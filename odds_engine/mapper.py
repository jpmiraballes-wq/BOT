from __future__ import annotations

from datetime import datetime, timezone
from rapidfuzz import fuzz
import re
import unicodedata

from config import Settings, settings as default_settings
from models import ExternalEvent, PolymarketMarket, MappingCandidate

ALIASES = {
    'man utd': 'manchester united',
    'man united': 'manchester united',
    'inter milan': 'internazionale',
    'psg': 'paris saint germain',
    'real madrid cf': 'real madrid',
}

DERIVATIVE_TERMS = [
    'regular time', 'regulation', 'first half', 'second half', 'spread',
    'handicap', 'over ', 'under ', 'total', 'score', 'round', 'method',
    'corner', 'card', 'points', 'goalscorer', 'assist', 'sets', 'map ',
]


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
    if any(x in q for x in DERIVATIVE_TERMS):
        return 'unsupported_derivative'
    if any(x in q for x in ['win the world cup', 'win ucl', 'win the champions league', 'champion', 'win the tournament']):
        return 'tournament_winner'
    if any(x in q for x in [' beat ', ' defeat ', ' wins ', ' win ']) or q.startswith('will '):
        return 'moneyline_full_event'
    return 'unknown'


def _name_score(name: str, text: str) -> float:
    n = normalize_name(name)
    if not n:
        return 0.0
    # Exact token containment is stronger than fuzzy partial ratio.
    if f' {n} ' in f' {text} ':
        return 1.0
    return max(fuzz.partial_ratio(n, text), fuzz.token_set_ratio(n, text)) / 100.0


def _participant_breakdown(event: ExternalEvent, market: PolymarketMarket) -> dict:
    q = normalize_name(market.question + ' ' + market.slug)
    home_score = _name_score(event.home_team, q)
    away_score = _name_score(event.away_team, q)
    both_present = home_score >= 0.78 and away_score >= 0.78
    # For head-to-head sports, one name is not enough. Use the weaker side as the main score.
    strict_score = min(home_score, away_score) if event.home_team and event.away_team else max(home_score, away_score)
    soft_score = (home_score + away_score) / 2.0 if event.home_team and event.away_team else strict_score
    return {
        'home_score': round(home_score, 4),
        'away_score': round(away_score, 4),
        'both_present': both_present,
        'strict_score': round(strict_score, 4),
        'soft_score': round(soft_score, 4),
    }


def _participant_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    return float(_participant_breakdown(event, market)['strict_score'])


def _sport_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    text = normalize_name(' '.join([event.sport_key, event.league, market.category, market.question]))
    if 'mma' in event.sport_key or 'ufc' in text:
        return 1.0 if any(x in text for x in ['mma', 'ufc', 'fight']) else 0.55
    if 'soccer' in event.sport_key or 'football' in text:
        return 1.0 if any(x in text for x in ['soccer', 'football', 'champions', 'premier', 'liga', 'cup']) else 0.55
    if any(x in event.sport_key for x in ['basketball', 'nba', 'americanfootball', 'nfl', 'tennis']):
        return 0.75
    return 0.5


def build_mapping_candidates(events: list[ExternalEvent], markets: list[PolymarketMarket], cfg: Settings | None = None) -> list[MappingCandidate]:
    runtime = cfg or default_settings
    candidates: list[MappingCandidate] = []
    for ev in events:
        for pm in markets:
            mtype = classify_market_type(pm.question)
            pbreak = _participant_breakdown(ev, pm)
            sport = _sport_score(ev, pm)
            participants = float(pbreak['strict_score'])
            timing = _time_score(ev, pm)
            market_type_score = 0.0 if mtype == 'unsupported_derivative' else (0.95 if mtype == 'moneyline_full_event' else (0.70 if mtype == 'tournament_winner' else 0.25))
            outcome_clarity = 0.85 if pm.yes_token_id and pm.best_ask is not None else 0.25

            # Hard guard: H2H event mapping requires both named participants. This prevents
            # false giant edges caused by matching only one fighter/team to an unrelated market.
            if ev.home_team and ev.away_team and not pbreak['both_present']:
                confidence = (0.15 * sport) + (0.55 * participants) + (0.10 * timing) + (0.10 * market_type_score) + (0.10 * outcome_clarity)
                if confidence < 0.55:
                    continue
                status = 'needs_review'
            else:
                confidence = (0.15 * sport) + (0.45 * participants) + (0.15 * timing) + (0.15 * market_type_score) + (0.10 * outcome_clarity)
                status = 'auto_approved' if confidence >= runtime.min_mapping_confidence and mtype == 'moneyline_full_event' else 'needs_review'

            candidates.append(MappingCandidate(
                external_event_id=ev.id,
                polymarket_market_id=pm.id,
                confidence_score=round(confidence, 4),
                status=status,
                market_type=mtype,
                outcome_mapping={
                    'yes_token_id': pm.yes_token_id,
                    'no_token_id': pm.no_token_id,
                    'external_outcome': ev.home_team,
                    'polymarket_outcome': pm.outcomes[0] if pm.outcomes else 'YES',
                    'note': 'strict two-participant H2H mapping; auto approval only when both sides match',
                },
                confidence_breakdown={
                    'sport': sport,
                    'participants': participants,
                    'home_score': pbreak['home_score'],
                    'away_score': pbreak['away_score'],
                    'both_present': pbreak['both_present'],
                    'time': timing,
                    'market_type': market_type_score,
                    'outcome_clarity': outcome_clarity,
                    'question': pm.question,
                },
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
