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
]

MONEYLINE_TERMS = [
    ' beat ', ' defeat ', ' defeats ', ' win ', ' wins ', ' vs ', ' v ', ' against ',
]

# SAFE_RELAXED_V1 — non-H2H markets that must NEVER be considered tradable
# even if both team names happen to appear (e.g. 'Will Real Madrid win La Liga?').
NON_H2H_TERMS = [
    ' win the league ', ' win league ', ' league winner ',
    ' win la liga ', ' win the premier league ', ' premier league winner ',
    ' champion ', ' champions ', ' championship ', ' title ',
    ' world series ', ' super bowl ', ' stanley cup ', ' nba finals ',
    ' top scorer ', ' golden boot ', ' mvp ', ' rookie of the year ',
    ' relegated ', ' relegation ', ' promoted ', ' promotion ',
    ' playoffs ', ' playoff ', ' make playoffs ', ' miss playoffs ',
    ' transfer ', ' signs for ', ' signs with ', ' joins ',
    ' next manager ', ' next coach ', ' fired ',
    ' election ', ' president ', ' senate ', ' congress ', ' politic ',
    ' nominee ', ' primary ',
    ' future ', ' futures ', ' outright ', ' to win ',
    ' season win total ', ' wins this season ',
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
        # SAFE_RELAXED_V1: include safe_relaxed_h2h.
        return self.label in ('exact_h2h_moneyline', 'safe_relaxed_h2h')


def validate_market(event: ExternalEvent, market: PolymarketMarket) -> ValidationResult:
    text = normalize_text(' '.join([market.question, market.slug, market.category]))
    home_score = _name_score(event.home_team, text)
    away_score = _name_score(event.away_team, text)
    both_present = home_score >= 0.84 and away_score >= 0.84
    both_present_strong = home_score >= 0.92 and away_score >= 0.92
    moneyline = any(term in f' {text} ' for term in MONEYLINE_TERMS) or text.startswith('will ')
    derivative = any(term in f' {text} ' for term in DERIVATIVE_TERMS)
    # SAFE_RELAXED_V1: hard blacklist for futures / outrights / championship / politics / transfer.
    non_h2h = any(term in f' {text} ' for term in NON_H2H_TERMS)

    if non_h2h:
        label = 'unrelated'
        reason = 'non_h2h_market_blacklisted'
    elif derivative:
        label = 'derivative_prop'
        reason = 'derivative_terms_detected'
    elif both_present and moneyline:
        label = 'exact_h2h_moneyline'
        reason = 'both_participants_and_moneyline_language'
    elif both_present_strong and not derivative:
        # SAFE_RELAXED_V1: both teams present with very high name scores,
        # no derivative terms and no non-H2H terms. Treat as a safe H2H.
        label = 'safe_relaxed_h2h'
        reason = 'both_participants_strong_match_no_derivative_no_futures'
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
