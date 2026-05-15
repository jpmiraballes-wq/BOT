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
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _similar(a: str, b: str) -> float:
    aa = _norm(a)
    bb = _norm(b)
    if not aa or not bb:
        return 0.0
    if aa == bb or f' {aa} ' in f' {bb} ' or f' {bb} ' in f' {aa} ':
        return 1.0
    return max(fuzz.partial_ratio(aa, bb), fuzz.token_set_ratio(aa, bb)) / 100.0


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


def _binary_side_from_question(market: PolymarketMarket, outcome_name: str) -> int | None:
    """Infer YES/NO token side for binary H2H questions like 'Team A vs Team B'.

    Polymarket often stores outcomes as Yes/No while the real team names live only
    in the question/slug. For a clean H2H market we map first-mentioned team to
    YES and second-mentioned team to NO. If confidence is not clear, return None.
    """
    question = market.question or market.slug or ''
    parts = re.split(r'\s+(?:vs\.?|v\.?|versus)\s+', question, flags=re.I)
    if len(parts) < 2:
        return None
    left = parts[0]
    right = parts[1]
    # Trim odds/market suffixes without being too clever.
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


def _token_and_price_for_outcome(market: PolymarketMarket, outcome_name: str) -> tuple[str, float, str]:
    idx = _match_outcome_index(market, outcome_name)
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
    token_id, price, outcome_label = _token_and_price_for_outcome(market, odds.outcome_name)
    spread = float(market.spread or 0.0)
    edge = fair_value - price
    edge_neto = edge - spread
    result = risk.validate_signal_inputs(mapping, market, odds, fair_value, price, edge)
    if not token_id:
        result.approved = False
        result.reason = 'missing_matched_outcome_token'
    action = 'BUY' if result.approved else 'IGNORE'
    explanation = (
        f'outcome={outcome_label} fair={fair_value:.4f} polymarket_price={price:.4f} '
        f'edge={edge:.4f} spread={spread:.4f} mapping={mapping.confidence_score:.3f} '
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
