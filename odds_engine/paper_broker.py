from __future__ import annotations

from config import settings
from models import Signal, PaperTrade, stable_id, now_iso
from risk_manager import RiskManager


class PaperBroker:
    def __init__(self, risk: RiskManager) -> None:
        self.risk = risk

    def open_from_signal(self, signal: Signal) -> PaperTrade | None:
        if settings.bot_mode != 'PAPER':
            return None
        if signal.action != 'BUY' or signal.risk_status != 'approved':
            return None
        if not signal.token_id or signal.polymarket_price <= 0:
            return None
        size_usd = float(settings.paper_trade_usd)
        qty = size_usd / signal.polymarket_price
        self.risk.reserve_paper_exposure(size_usd)
        return PaperTrade(
            id=stable_id('paper', signal.id),
            signal_id=signal.id,
            external_event_id=signal.external_event_id,
            polymarket_market_id=signal.polymarket_market_id,
            token_id=signal.token_id,
            side='YES',
            entry_price=signal.polymarket_price,
            exit_price=None,
            size_usd=size_usd,
            quantity=round(qty, 6),
            status='open',
            pnl_usd=0.0,
            pnl_pct=0.0,
            opened_at=now_iso(),
            closed_at=None,
            reason_open=signal.explanation,
            reason_close=None,
        )
