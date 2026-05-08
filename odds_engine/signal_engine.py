from __future__ import annotations

from models import Signal, MappingCandidate, PolymarketMarket, OddsOutcome, stable_id, now_iso
from risk_manager import RiskManager
from config import settings


def build_buy_signal(
    mapping: MappingCandidate,
    market: PolymarketMarket,
    odds: OddsOutcome,
    fair_value: float,
    risk: RiskManager,
) -> Signal:
    token_id = market.yes_token_id or ''
    price = float(market.best_ask or 0.0)
    spread = float(market.spread or 0.0)
    edge = fair_value - price
    # edge neto simple: descuenta spread completo. Luego podemos agregar slippage/model error.
    edge_neto = edge - spread
    result = risk.validate_signal_inputs(mapping, market, odds, fair_value, price, edge)
    action = 'BUY' if result.approved else 'IGNORE'
    explanation = (
        f'fair={fair_value:.4f} polymarket_ask={price:.4f} '
        f'edge={edge:.4f} spread={spread:.4f} mapping={mapping.confidence_score:.3f} '
        f'risk={result.reason}'
    )
    return Signal(
        id=stable_id(mapping.external_event_id, market.id, token_id, now_iso()),
        strategy='odds_mispricing_v1',
        external_event_id=mapping.external_event_id,
        polymarket_market_id=market.id,
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
        mode=settings.bot_mode,
        explanation=explanation,
        created_at=now_iso(),
    )
