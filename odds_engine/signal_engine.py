from __future__ import annotations

import re
import unicodedata
from rapidfuzz import fuzz

from models import Signal, MappingCandidate, PolymarketMarket, OddsOutcome, stable_id, now_iso
from risk_manager import RiskManager
from config import Settings, settings as default_settings


def _norm(value: str) -> str:
    s = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'[^a-z0-9 +\-.]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _line(value: str) -> str:
    m = re.search(r'(?<!\d)([+-]?\d+(?:\.\d+)?|[+-]?\d+pt\d+)(?!\d)', value or '', re.I)
    if not m:
        return ''
    raw = m.group(1).lower().replace('pt', '.')
    try:
        x = float(raw)
        if x.is_integer():
            return str(int(x))
        return str(x).rstrip('0').rstrip('.')
    except Exception:
        return raw


def _similar(a: str, b: str) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    if aa == bb or f' {aa} ' in f' {bb} ' or f' {bb} ' in f' {aa} ':
        return 1.0
    return max(fuzz.partial_ratio(aa, bb), fuzz.token_set_ratio(aa, bb)) / 100.0


def _is_binary_yes_no(market: PolymarketMarket) -> bool:
    outcomes = [_norm(x) for x in (market.outcomes or [])]
    return len(outcomes) >= 2 and outcomes[0] == 'yes' and outcomes[1] == 'no'


def _match_outcome_index(market: PolymarketMarket, outcome_name: str) -> int | None:
    target = _norm(outcome_name)
    if not target:
        return None
    best_i = None
    best_score = 0.0
    for i, outcome in enumerate(market.outcomes or []):
        cand = _norm(outcome)
        if not cand:
            continue
        if cand == target:
            return i
        score = max(fuzz.partial_ratio(target, cand), fuzz.token_set_ratio(target, cand)) / 100.0
        if score > best_score:
            best_score = score
            best_i = i
    return best_i if best_score >= 0.84 else None


def _binary_side_from_derivative(mapping: MappingCandidate, market: PolymarketMarket, outcome_name: str) -> int | None:
    """Infer YES/NO side for PAPER-only exact totals/spreads.

    For binary Polymarket questions with outcomes Yes/No:
    - '... O/U 2.5' maps Over 2.5 to YES, Under 2.5 to NO.
    - 'Spread: Team (-1.5)' maps Team -1.5 to YES, the opposite side to NO.

    This is intentionally narrow and only runs for mapping.market_type values
    produced by PAPER_ONLY_DERIVATIVES_V1.
    """
    mtype = str(getattr(mapping, 'market_type', '') or '')
    q = _norm((market.question or '') + ' ' + (market.slug or ''))
    out = _norm(outcome_name or '')
    if mtype == 'total_exact':
        if _line(q) != _line(out):
            return None
        if out.startswith('over '):
            return 0
        if out.startswith('under '):
            return 1
        return None

    if mtype == 'spread_exact':
        qline = _line(q).lstrip('+')
        oline = _line(out).lstrip('+')
        if not qline or qline != oline:
            return None
        # If the Odds API outcome team appears in the Polymarket spread question,
        # buy YES; otherwise buy NO. This handles markets written as a single
        # spread side like 'Spread: Manchester City FC (-1.5)'.
        team_part = re.sub(r'\s*[+-]?\d+(?:\.\d+)?\s*$', '', out).strip()
        if team_part and _similar(team_part, q) >= 0.84:
            return 0
        return 1

    return None


def _binary_side_from_question(market: PolymarketMarket, outcome_name: str) -> int | None:
    """Infer YES/NO token side for binary H2H questions.

    Polymarket often stores outcomes as Yes/No while the real selection lives in
    the question/slug. For clean 'Team A vs Team B' questions we map the first
    mentioned team to YES and second to NO. For binary draw markets like
    'Will Team A vs Team B end in a draw?' we map Odds API 'Draw' to YES.
    """
    question = market.question or market.slug or ''
    q_norm = _norm(question)
    out_norm = _norm(outcome_name)

    if _is_binary_yes_no(market) and out_norm == 'draw' and 'draw' in q_norm:
        return 0

    parts = re.split(r'\s+(?:vs\.?|v\.?|versus)\s+', question, flags=re.I)
    if len(parts) < 2:
        return None
    left = parts[0]
    right = parts[1]
    right = re.split(r'\?|\(|\[|\{| - | — |:', right, maxsplit=1)[0]
    left_score = _similar(outcome_name, left)
    right_score = _similar(outcome_name, right)
    if left_score >= 0.84 and left_score - right_score >= 0.08:
        return 0
    if right_score >= 0.84 and right_score - left_score >= 0.08:
        return 1
    return None


def _price_for_index(market: PolymarketMarket, idx: int) -> float:
    if idx < len(market.outcome_prices):
        return float(market.outcome_prices[idx] or 0.0)
    if idx == 0 and market.best_ask is not None:
        return float(market.best_ask or 0.0)
    if idx == 1 and market.best_bid is not None:
        return max(0.01, min(0.99, 1.0 - float(market.best_bid or 0.0)))
    return 0.0


def _token_for_index(market: PolymarketMarket, idx: int) -> str:
    if idx < len(market.token_ids):
        return str(market.token_ids[idx] or '')
    if idx == 0 and market.yes_token_id:
        return str(market.yes_token_id)
    if idx == 1 and market.no_token_id:
        return str(market.no_token_id)
    return ''


def _token_and_price_for_outcome(mapping: MappingCandidate, market: PolymarketMarket, outcome_name: str) -> tuple[str, float, str]:
    idx = _match_outcome_index(market, outcome_name)
    if idx is None:
        idx = _binary_side_from_derivative(mapping, market, outcome_name)
    if idx is None:
        idx = _binary_side_from_question(market, outcome_name)
    if idx is None:
        return '', 0.0, outcome_name
    token_id = _token_for_index(market, idx)
    outcome_label = outcome_name
    if market.outcomes and idx < len(market.outcomes):
        raw_label = str(market.outcomes[idx] or '')
        if _norm(raw_label) not in {'yes', 'no'}:
            outcome_label = raw_label
    price = _price_for_index(market, idx)
    return token_id, price, outcome_label


def build_buy_signal(
    mapping: MappingCandidate,
    market: PolymarketMarket,
    odds: OddsOutcome,
    fair_value: float,
    risk: RiskManager,
    cfg: Settings | None = None,
) -> Signal:
    runtime = cfg or default_settings
    token_id, price, outcome_label = _token_and_price_for_outcome(mapping, market, odds.outcome_name)
    spread = float(market.spread or 0.0)
    edge = fair_value - price
    edge_neto = edge - spread
    result = risk.validate_signal_inputs(mapping, market, odds, fair_value, price, edge)
    if not token_id:
        result.approved = False
        result.reason = 'missing_matched_outcome_token'
    odds_age = risk.odds_age_seconds(odds)
    odds_ttl = risk.odds_ttl_seconds()
    odds_age_text = 'unknown' if odds_age is None else f'{odds_age:.1f}'
    action = 'BUY' if result.approved else 'IGNORE'
    explanation = (
        f'outcome={outcome_label} fair={fair_value:.4f} polymarket_price={price:.4f} '
        f'edge={edge:.4f} spread={spread:.4f} mapping={mapping.confidence_score:.3f} '
        f'odds_age={odds_age_text}s odds_ttl={odds_ttl}s '
        f'token={token_id[:10] if token_id else "MISSING"} risk={result.reason}'
    )
    return Signal(
        id=stable_id('signal', mapping.external_event_id, market.id, token_id or odds.outcome_name),
        strategy='odds_mispricing_v1',
        external_event_id=mapping.external_event_id,
        polymarket_market_id=market.id,
        outcome=outcome_label,
        token_id=token_id,
        action=action,
        fair_value=round(fair_value, 6),
        polymarket_price=round(price, 6),
        edge_bruto=round(edge, 6),
        edge_neto=round(edge_neto, 6),
        spread=round(spread, 6),
        liquidity=float(market.liquidity or 0.0),
        confidence=float(mapping.confidence_score),
        freshness_status='fresh' if risk.odds_are_fresh(odds) else 'stale',
        mapping_status=mapping.status,
        risk_status='approved' if result.approved else 'rejected',
        reject_reason='' if result.approved else result.reason,
        mode=runtime.bot_mode,
        explanation=explanation,
        created_at=now_iso(),
    )
