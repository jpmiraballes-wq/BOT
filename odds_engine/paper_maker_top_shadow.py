from __future__ import annotations

import json
import logging

from paper_maker_engine import BookTop, MakerConfig
from paper_maker_god import GodModePaperMaker
from paper_maker_execution_audit import run_execution_audit
from paper_maker_queue_audit import run_queue_audit
from paper_maker_live_shadow import run_live_shadow_audit

log = logging.getLogger('paper_maker_top_shadow')


class TopNearQuotePaperMaker(GodModePaperMaker):
    """V9.1 paper-only quote placement focused on live-shadow realism.

    This runner does not send live orders. It changes only paper quote placement:
    instead of spreading many levels away from touch, it posts tiny quotes one tick
    behind the executable touch so strict markout keeps an exit edge while the
    live-shadow/queue audits can measure whether the bot is actually near top.
    """

    def __init__(self) -> None:
        cfg = MakerConfig(
            quote_levels=2,
            micro_cycles=2,
            min_orders_per_cycle=500,
            max_markets=20,
            book_fetch_limit=80,
            small_order_size_usd=1.0,
            min_order_size_usd=1.0,
            max_order_size_usd=3.0,
            block_order_size_usd=3.0,
            block_every_n_quotes=0,
        )
        super().__init__(cfg)
        self.strict_max_order_size_usd = min(self.strict_max_order_size_usd, 3.0)
        self.strict_level0_order_size_usd = min(self.strict_level0_order_size_usd, 1.0)
        self.queue_max_notional_ok = max(self.queue_max_notional_ok, 250.0)
        self.queue_max_multiple_ok = max(self.queue_max_multiple_ok, 60.0)
        self.queue_min_partial_ok = min(self.queue_min_partial_ok, 0.02)

    def _quote_price(self, b: BookTop, side: str, level: int, micro: int) -> float:
        tick = b.tick_size or 0.01
        # Keep one-tick executable exit edge. This is the closest strict paper can
        # get to live top-of-book without pretending that crossing/zero-edge fills
        # are profitable.
        if side == 'BUY':
            px = b.best_bid - tick * max(1, level + (micro % 2))
            return self._round_price(min(px, b.best_ask - tick), tick)
        px = b.best_ask + tick * max(1, level + (micro % 2))
        return self._round_price(max(px, b.best_bid + tick), tick)

    def _order_size(self, quote_index: int, level: int) -> float:
        return 1.0 if level == 0 else 1.5


def run_top_near_shadow_cycle() -> dict:
    maker = TopNearQuotePaperMaker().run_once()
    execution = run_execution_audit()
    queue = run_queue_audit()
    shadow = run_live_shadow_audit()
    return {
        'mode': 'V9_1_TOP_NEAR_SHADOW_CYCLE',
        'paper_only': True,
        'live_orders_enabled': False,
        'maker': {
            'orders_simulated_today': maker.get('orders_simulated_today'),
            'fills_simulated_today': maker.get('fills_simulated_today'),
            'open_quotes': maker.get('open_quotes'),
            'maker_total_pnl_usd': maker.get('maker_total_pnl_usd'),
            'inventory_exposure_usd': maker.get('inventory_exposure_usd'),
            'strict_fill_rejects': maker.get('strict_fill_rejects'),
            'strict_v6_controls': maker.get('strict_v6_controls'),
        },
        'execution': {
            'verdict': execution.get('verdict'),
            'execution_ok_rate': execution.get('execution_ok_rate'),
            'avg_execution_risk_score': execution.get('avg_execution_risk_score'),
        },
        'queue': {
            'verdict': queue.get('verdict'),
            'fills_measured_for_queue': queue.get('fills_measured_for_queue'),
            'quote_at_top_or_better_rate': queue.get('quote_at_top_or_better_rate'),
            'fill_at_top_or_better_rate': queue.get('fill_at_top_or_better_rate'),
            'quote_queue_risk_rate': queue.get('quote_queue_risk_rate'),
            'fill_queue_risk_rate': queue.get('fill_queue_risk_rate'),
            'queue_adjusted_executable_markout_usd': queue.get('queue_adjusted_executable_markout_usd'),
        },
        'live_shadow_v9': {
            'verdict': shadow.get('verdict'),
            'shadow_fills': shadow.get('shadow_fills'),
            'at_top_rate': shadow.get('at_top_rate'),
            'near_top_rate': shadow.get('near_top_rate'),
            'avg_queue_ahead_notional_usd': shadow.get('avg_queue_ahead_notional_usd'),
            'avg_expected_fill_probability': shadow.get('avg_expected_fill_probability'),
            'queue_adjusted_shadow_pnl_usd': shadow.get('queue_adjusted_shadow_pnl_usd'),
            'queue_adjusted_roi_on_rotated_notional': shadow.get('queue_adjusted_roi_on_rotated_notional'),
            'warnings': shadow.get('warnings'),
        },
        'official_paper_metric': 'live_shadow_v9.queue_adjusted_shadow_pnl_usd',
        'interpretation': 'V9.1 tests whether strict one-tick-behind quotes can be near-top/queue-plausible. It still sends no live orders.',
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_top_near_shadow_cycle(), ensure_ascii=False, indent=2, default=str))
