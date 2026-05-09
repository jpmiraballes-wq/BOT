from __future__ import annotations

import json
import logging

from config import Settings, settings as default_settings
from models import Signal, PaperTrade, stable_id, now_iso
from risk_manager import RiskManager

log = logging.getLogger(__name__)


class PaperBroker:
    def __init__(self, risk: RiskManager, cfg: Settings | None = None) -> None:
        self.risk = risk
        self.cfg = cfg or default_settings

    def _has_open_position(self, token_id: str, side: str = 'YES') -> bool:
        """Return True when the paper book already has an open position on this token/side.

        Signal IDs can legitimately differ across scans for the same Polymarket token.
        The paper broker must dedupe by position identity, not only by trade ID.
        """
        if not token_id:
            return False
        path = self.cfg.data_dir / 'papertrade.jsonl'
        if not path.exists():
            return False
        try:
            for line in path.read_text(errors='ignore').splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('status') != 'open':
                    continue
                if str(row.get('token_id') or '') == str(token_id) and str(row.get('side') or 'YES') == side:
                    return True
        except Exception as exc:
            log.warning('paper_open_position_check_failed token=%s err=%s', str(token_id)[:10], exc)
        return False

    def open_from_signal(self, signal: Signal) -> PaperTrade | None:
        if self.cfg.bot_mode != 'PAPER':
            return None
        if signal.action != 'BUY' or signal.risk_status != 'approved':
            return None
        if not signal.token_id or signal.polymarket_price <= 0:
            return None
        if self._has_open_position(signal.token_id, 'YES'):
            log.info(
                'paper_trade_duplicate_token_skipped token=%s signal=%s market=%s',
                str(signal.token_id)[:10], signal.id, signal.polymarket_market_id,
            )
            return None
        size_usd = float(self.cfg.paper_trade_usd)
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

    def force_test_trade_from_signal(self, signal: Signal) -> PaperTrade | None:
        """Force ONE paper trade for pipeline validation. PAPER mode only.

        Bypasses MIN_EDGE / risk approval but still requires:
          - bot_mode == 'PAPER'
          - mapping_status == 'auto_approved'
          - valid token_id and polymarket_price
        Marks the trade with is_test_trade=True and test_reason.
        """
        if self.cfg.bot_mode != 'PAPER':
            return None
        if not self.cfg.paper_force_test_trade:
            return None
        if getattr(signal, 'mapping_status', None) != 'auto_approved':
            return None
        if not signal.token_id or signal.polymarket_price <= 0:
            return None
        if self._has_open_position(signal.token_id, 'YES'):
            log.info(
                'paper_test_trade_duplicate_token_skipped token=%s signal=%s market=%s',
                str(signal.token_id)[:10], signal.id, signal.polymarket_market_id,
            )
            return None
        size_usd = float(self.cfg.test_trade_size_usd)
        qty = size_usd / signal.polymarket_price
        self.risk.reserve_paper_exposure(size_usd)
        trade = PaperTrade(
            id=stable_id('paper_test', signal.id),
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
            reason_open=f'forced_paper_pipeline_test edge_neto={signal.edge_neto}',
            reason_close=None,
        )
        # Best-effort runtime markers (entity may ignore unknown attrs).
        try:
            setattr(trade, 'is_test_trade', True)
            setattr(trade, 'test_reason', 'forced_paper_pipeline_test')
            setattr(trade, 'risk_status', 'test_approved')
            setattr(trade, 'reject_reason', None)
        except Exception:
            pass
        return trade
