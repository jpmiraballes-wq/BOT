from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from config import Settings, settings as default_settings
from models import MappingCandidate, PolymarketMarket, OddsOutcome


@dataclass
class RiskResult:
    approved: bool
    reason: str


class RiskManager:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.open_exposure_usd = 0.0

    def odds_are_fresh(self, outcome: OddsOutcome) -> bool:
        ts = outcome.provider_last_update or outcome.received_at
        try:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return age <= self.cfg.default_odds_ttl_seconds
        except Exception:
            return False

    def validate_signal_inputs(
        self,
        mapping: MappingCandidate,
        market: PolymarketMarket,
        odds: OddsOutcome,
        fair_value: float,
        polymarket_price: float,
        edge: float,
    ) -> RiskResult:
        if self.cfg.bot_mode not in {'OBSERVE', 'PAPER'}:
            return RiskResult(False, 'live_modes_blocked_in_v1')
        if mapping.status != 'auto_approved':
            return RiskResult(False, 'mapping_not_auto_approved')
        if mapping.confidence_score < self.cfg.min_mapping_confidence:
            return RiskResult(False, 'mapping_confidence_below_threshold')
        if mapping.market_type == 'unsupported_derivative':
            return RiskResult(False, 'unsupported_market_type')
        if not self.odds_are_fresh(odds):
            return RiskResult(False, 'stale_odds')
        if market.best_ask is None or market.best_bid is None:
            return RiskResult(False, 'missing_polymarket_prices')
        if market.spread is None or market.spread > self.cfg.max_spread:
            return RiskResult(False, 'spread_too_wide')
        if market.liquidity < self.cfg.min_liquidity:
            return RiskResult(False, 'liquidity_too_low')
        if not (0.01 <= polymarket_price <= 0.99):
            return RiskResult(False, 'bad_polymarket_price')
        if not (0.01 <= fair_value <= 0.99):
            return RiskResult(False, 'bad_fair_value')
        if edge < self.cfg.min_edge:
            return RiskResult(False, 'edge_below_threshold')
        if self.open_exposure_usd + self.cfg.paper_trade_usd > self.cfg.max_total_exposure_usd:
            return RiskResult(False, 'paper_exposure_cap_reached')
        return RiskResult(True, 'approved')

    def reserve_paper_exposure(self, size_usd: float) -> None:
        self.open_exposure_usd += max(0.0, float(size_usd))
