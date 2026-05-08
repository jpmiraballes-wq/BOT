from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from rapidfuzz import fuzz

from models import ExternalEvent, PolymarketMarket

DERIVATIVE_TERMS = [
    'ko', 'tko', 'submission', 'decision', 'method', 'round', 'rounds',
    'over', 'under', 'total', 'spread', 'handicap', 'points', 'goals',
    'first half', 'second half', 'regular time', 'regulation', 'corner',
    'card', 'cards', 'score', 'finish', 'finishes', 'by ', 'margin',
    # Important safety: draw/tie markets are not team moneyline markets.
    # Example: "Will Team A vs Team B end in a draw?" must never map to Team A/Team B.
    ' draw ', ' tie ', ' tied ', ' stalemate ',
]

MONEYLINE_TERMS = [
    ' beat ', ' defeat ', ' defeats ', ' win ', ' wins ', ' vs ', ' v ', ' against ',
]


def normalize_text(value: str) -> str:
    s = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(r'\b(fc|cf|club|the|team)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _name_score(name: str, text: str) -> float:
    n = normalize_text(name)
    if not n:
        return 0.0
    wrapped = f' {text} '
    if f' {n} ' in wrapped:
        return 1.0
    parts = [p for p in n.split() if len(p) >= 3]
    partial = max(fuzz.partial_ratio(n, text), fuzz.token_set_ratio(n, text)) / 100.0
    if parts:
        last = parts[-1]
        if f' {last} ' in wrapped:
            partial = max(partial, 0.82)
    return partial


@dataclass
class ValidationResult:
    label: str
    home_score: float
    away_score: float
    both_participants_present: bool
    moneyline_language: bool
    derivative_detected: bool
    reason: str

    @property
    def tradable(self) -> bool:
        return self.label == 'exact_h2h_moneyline'


def validate_market(event: ExternalEvent, market: PolymarketMarket) -> ValidationResult:
    text = normalize_text(' '.join([market.question, market.slug, market.category]))
    home_score = _name_score(event.home_team, text)
    away_score = _name_score(event.away_team, text)
    both_present = home_score >= 0.84 and away_score >= 0.84
    moneyline = any(term in f' {text} ' for term in MONEYLINE_TERMS) or text.startswith('will ')
    derivative = any(term in f' {text} ' for term in DERIVATIVE_TERMS)

    if derivative:
        label = 'derivative_prop'
        reason = 'derivative_terms_detected'
    elif both_present and moneyline:
        label = 'exact_h2h_moneyline'
        reason = 'both_participants_and_moneyline_language'
    elif both_present:
        label = 'likely_h2h'
        reason = 'both_participants_but_moneyline_language_unclear'
    else:
        label = 'unrelated'
        reason = 'missing_one_or_both_participants'

    return ValidationResult(
        label=label,
        home_score=round(home_score, 4),
        away_score=round(away_score, 4),
        both_participants_present=both_present,
        moneyline_language=moneyline,
        derivative_detected=derivative,
        reason=reason,
    )
