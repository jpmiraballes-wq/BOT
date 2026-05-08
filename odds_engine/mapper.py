from __future__ import annotations

from datetime import datetime, timezone
from rapidfuzz import fuzz
import re
import unicodedata

from config import Settings, settings as default_settings
from models import ExternalEvent, PolymarketMarket, MappingCandidate
from market_validator import validate_market

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
    if f' {n} ' in f' {text} ':
        return 1.0
    return max(fuzz.partial_ratio(n, text), fuzz.token_set_ratio(n, text)) / 100.0


def _participant_breakdown(event: ExternalEvent, market: PolymarketMarket) -> dict:
    q = normalize_name(market.question + ' ' + market.slug)
    home_score = _name_score(event.home_team, q)
    away_score = _name_score(event.away_team, q)
    both_present = home_score >= 0.78 and away_score >= 0.78
    strict_score = min(home_score, away_score) if event.home_team and event.away_team else max(home_score, away_score)
    soft_score = (home_score + away_score) / 2.0 if event.home_team and event.away_team else strict_score
    return {
        'home_score': round(home_score, 4),
        'away_score': round(away_score, 4),
        'both_present': both_present,
        'strict_score': round(strict_score, 4),
        'soft_score': round(soft_score, 4),
    }


def _sport_score(event: ExternalEvent, market: PolymarketMarket) -> float:
    text = normalize_name(' '.join([event.sport_key, event.league, market.category, market.question]))
    if 'mma' in event.sport_key or 'ufc' in text:
        return 1.0 if any(x in text for x in ['mma', 'ufc', 'fight']) else 0.55
    if 'soccer' in event.sport_key or 'football' in text:
        return 1.0 if any(x in text for x in ['soccer', 'football', 'champions', 'premier', 'liga', 'cup']) else 0.55
    if any(x in event.sport_key for x in ['basketball', 'nba', 'americanfootball', 'nfl', 'tennis', 'baseball', 'mlb']):
        return 0.75
    return 0.5


def build_mapping_candidates(events: list[ExternalEvent], markets: list[PolymarketMarket], cfg: Settings | None = None) -> list[MappingCandidate]:
    runtime = cfg or default_settings
    candidates: list[MappingCandidate] = []
    for ev in events:
        for pm in markets:
            validator = validate_market(ev, pm)
            if validator.label == 'unrelated':
                continue

            mtype = classify_market_type(pm.question)
            pbreak = _participant_breakdown(ev, pm)
            sport = _sport_score(ev, pm)
            participants = max(float(pbreak['strict_score']), min(validator.home_score, validator.away_score))
            timing = _time_score(ev, pm)
            outcome_clarity = 0.85 if pm.yes_token_id else 0.25

            if validator.label == 'derivative_prop' or mtype == 'unsupported_derivative':
                market_type_score = 0.0
                confidence = (0.15 * sport) + (0.45 * participants) + (0.15 * timing) + (0.15 * market_type_score) + (0.10 * outcome_clarity)
                if confidence < 0.70:
                    continue
                status = 'needs_review'
                mtype = 'unsupported_derivative'
            elif validator.label == 'exact_h2h_moneyline':
                market_type_score = 1.0
                confidence = (0.15 * sport) + (0.50 * participants) + (0.15 * timing) + (0.10 * market_type_score) + (0.10 * outcome_clarity)
                if validator.home_score >= 0.98 and validator.away_score >= 0.98:
                    confidence = max(confidence, 0.88)
                status = 'auto_approved' if confidence >= min(runtime.min_mapping_confidence, 0.88) else 'needs_review'
                mtype = 'moneyline_full_event'
            elif validator.label == 'safe_relaxed_h2h':
                # SAFE_RELAXED_V1: both team names match strongly, no derivative
                # terms, no futures/outrights. Auto-approve with a STRICTER
                # confidence floor than exact_h2h_moneyline (0.90 vs 0.88) AND
                # require both name scores >= 0.92.
                market_type_score = 0.85
                confidence = (0.15 * sport) + (0.50 * participants) + (0.15 * timing) + (0.10 * market_type_score) + (0.10 * outcome_clarity)
                strong_names = validator.home_score >= 0.92 and validator.away_score >= 0.92
                if strong_names:
                    status = 'auto_approved' if confidence >= min(runtime.min_mapping_confidence, 0.90) else 'needs_review'
                else:
                    status = 'needs_review'
                mtype = 'moneyline_full_event'
            elif validator.label == 'likely_h2h':
                market_type_score = 0.65
                confidence = (0.15 * sport) + (0.48 * participants) + (0.15 * timing) + (0.12 * market_type_score) + (0.10 * outcome_clarity)
                status = 'needs_review'
            else:
                continue

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
                    'validator_label': validator.label,
                    'validator_reason': validator.reason,
                    'note': 'strict validator: auto approval only for exact H2H moneyline markets',
                },
                confidence_breakdown={
                    'sport': sport,
                    'participants': participants,
                    'home_score': max(pbreak['home_score'], validator.home_score),
                    'away_score': max(pbreak['away_score'], validator.away_score),
                    'both_present': pbreak['both_present'] or validator.both_participants_present,
                    'time': timing,
                    'market_type': market_type_score,
                    'outcome_clarity': outcome_clarity,
                    'validator_label': validator.label,
                    'validator_reason': validator.reason,
                    'derivative_detected': validator.derivative_detected,
                    'question': pm.question,
                },
            ))
    candidates.sort(key=lambda x: x.confidence_score, reverse=True)
    return candidates


def score_event_against_markets(event: ExternalEvent, markets: list[PolymarketMarket], runtime: Settings | None = None, k: int = 3) -> list[dict]:
    """Diagnostic helper: rank ALL markets against an event using the same
    scoring used by build_mapping_candidates, but WITHOUT filtering them out.

    Returns the top-k (by confidence) with full breakdown so we can answer:
      "this event had no candidate because the closest market was X with
      validator_label=Y, both_present=Z, etc."
    """
    cfg = runtime or default_settings
    rows = []
    for pm in markets:
        try:
            validator = validate_market(event, pm)
            mtype = classify_market(pm.question)
            pbreak = _participant_breakdown(event, pm)
            sport = _sport_score(event, pm)
            participants = max(float(pbreak['strict_score']), min(validator.home_score, validator.away_score))
            timing = _time_score(event, pm)
            outcome_clarity = 0.85 if pm.yes_token_id else 0.25

            if validator.label == 'exact_h2h_moneyline':
                market_type_score = 1.0
                confidence = (0.15 * sport) + (0.50 * participants) + (0.15 * timing) + (0.10 * market_type_score) + (0.10 * outcome_clarity)
            elif validator.label == 'likely_h2h':
                market_type_score = 0.65
                confidence = (0.15 * sport) + (0.48 * participants) + (0.15 * timing) + (0.12 * market_type_score) + (0.10 * outcome_clarity)
            elif validator.label == 'derivative_prop':
                market_type_score = 0.0
                confidence = (0.15 * sport) + (0.45 * participants) + (0.15 * timing) + (0.15 * market_type_score) + (0.10 * outcome_clarity)
            else:
                market_type_score = 0.0
                confidence = (0.15 * sport) + (0.40 * participants) + (0.15 * timing) + (0.05 * market_type_score) + (0.10 * outcome_clarity)

            rows.append({
                'polymarket_market_id': pm.id,
                'question': pm.question,
                'slug': pm.slug,
                'confidence': round(confidence, 4),
                'home_score': round(max(pbreak['home_score'], validator.home_score), 4),
                'away_score': round(max(pbreak['away_score'], validator.away_score), 4),
                'both_present': bool(pbreak['both_present'] or validator.both_participants_present),
                'partial_strict_score': round(pbreak['strict_score'], 4),
                'partial_soft_score': round(pbreak['soft_score'], 4),
                'sport_score': round(sport, 4),
                'time_score': round(timing, 4),
                'validator_label': validator.label,
                'validator_reason': validator.reason,
                'derivative_detected': bool(validator.derivative_detected),
                'classified_market_type': mtype,
                'min_mapping_confidence': float(cfg.min_mapping_confidence),
                'reject_reason': _diagnostic_reject_reason(validator, confidence, cfg),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r['confidence'], reverse=True)
    return rows[: max(1, int(k))]


def _diagnostic_reject_reason(validator, confidence: float, cfg: Settings) -> str:
    """Plain-language reason a market did not become a mapping candidate."""
    if validator.label == 'derivative_prop':
        return 'derivative_or_prop_market'
    if validator.label == 'unrelated':
        return 'missing_one_or_both_participants'
    if validator.label == 'likely_h2h' and confidence < cfg.min_mapping_confidence:
        return 'h2h_likely_but_confidence_below_min_mapping_confidence'
    if validator.label == 'exact_h2h_moneyline' and confidence < min(cfg.min_mapping_confidence, 0.88):
        return 'h2h_exact_but_confidence_below_threshold'
    return 'ok_or_close'


def best_candidate_for_event(event_id: str, candidates: list[MappingCandidate]) -> MappingCandidate | None:
    matches = [c for c in candidates if c.external_event_id == event_id]
    if not matches:
        return None
    if len(matches) > 1 and matches[0].confidence_score - matches[1].confidence_score < 0.08:
        matches[0].status = 'needs_review'
    return matches[0]
