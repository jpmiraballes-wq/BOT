from __future__ import annotations

import json
import logging
import os

from paper_maker_engine import BookTop, MakerConfig, _float
from paper_maker_god import GodModePaperMaker
from paper_maker_execution_audit import run_execution_audit
from paper_maker_queue_audit import run_queue_audit
from paper_maker_live_shadow import run_live_shadow_audit

log = logging.getLogger('paper_maker_shallow_touch_shadow')


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _level_size(level: dict) -> float:
    return _float(level.get('size') or level.get('shares') or level.get('amount'))


class ShallowTouchPaperMaker(GodModePaperMaker):
    """V9.3 paper-only shallow queue touch test.

    Touch quoting proved the bot can sit at top, but the measured queue was still
    too deep. This runner only keeps books where the best bid/ask queue is small
    enough to plausibly get filled with tiny paper size. No live orders are sent.
    """

    def __init__(self) -> None:
        cfg = MakerConfig(
            quote_levels=1,
            micro_cycles=2,
            min_orders_per_cycle=500,
            max_markets=30,
            book_fetch_limit=120,
            small_order_size_usd=1.0,
            min_order_size_usd=1.0,
            max_order_size_usd=1.5,
            block_order_size_usd=1.5,
            block_every_n_quotes=0,
        )
        super().__init__(cfg)
        self.strict_max_order_size_usd = 1.5
        self.strict_level0_order_size_usd = 1.0
        self.shallow_max_touch_queue_usd = _float_env('PAPER_MAKER_SHALLOW_TOUCH_MAX_QUEUE_USD', 120.0)
        self.shallow_max_spread = _float_env('PAPER_MAKER_SHALLOW_TOUCH_MAX_SPREAD', 0.025)
        self.queue_max_notional_ok = min(max(self.queue_max_notional_ok, 120.0), 300.0)
        self.queue_max_multiple_ok = max(self.queue_max_multiple_ok, 80.0)
        self.queue_min_partial_ok = min(self.queue_min_partial_ok, 0.01)
        self.shallow_stats: dict[str, int] = {}

    def _touch_queue_metrics(self, b: BookTop) -> dict:
        book = self._book_for_token(b.token_id)
        if not book:
            return {'ok': False, 'reason': 'book_unavailable'}
        bids = self._book_levels(book, 'BUY')
        asks = self._book_levels(book, 'SELL')
        best_bid_level = bids[0] if bids else {}
        best_ask_level = asks[0] if asks else {}
        best_bid = _float(best_bid_level.get('price')) or b.best_bid
        best_ask = _float(best_ask_level.get('price')) or b.best_ask
        bid_queue_usd = _level_size(best_bid_level) * best_bid if best_bid_level else 999999999.0
        ask_queue_usd = _level_size(best_ask_level) * best_ask if best_ask_level else 999999999.0
        best_touch_queue_usd = min(bid_queue_usd, ask_queue_usd)
        spread = best_ask - best_bid if best_bid > 0 and best_ask > best_bid else b.spread
        ok = spread <= self.shallow_max_spread and best_touch_queue_usd <= self.shallow_max_touch_queue_usd
        return {
            'ok': ok,
            'reason': None if ok else 'touch_queue_too_deep_or_spread_wide',
            'bid_queue_usd': bid_queue_usd,
            'ask_queue_usd': ask_queue_usd,
            'best_touch_queue_usd': best_touch_queue_usd,
            'spread': spread,
        }

    def discover_books(self) -> list[BookTop]:
        books = super().discover_books()
        kept: list[BookTop] = []
        stats = {
            'strict_kept_before_shallow_filter': len(books),
            'kept_shallow_touch': 0,
            'book_unavailable': 0,
            'touch_queue_too_deep_or_spread_wide': 0,
        }
        for b in books:
            m = self._touch_queue_metrics(b)
            if not m.get('ok'):
                reason = str(m.get('reason') or 'touch_queue_too_deep_or_spread_wide')
                stats[reason] = stats.get(reason, 0) + 1
                continue
            kept.append(b)
        stats['kept_shallow_touch'] = len(kept)
        self.shallow_stats = stats
        log.info('paper_maker_shallow_touch_filter %s', stats)
        return kept

    def _quote_price(self, b: BookTop, side: str, level: int, micro: int) -> float:
        tick = b.tick_size or 0.01
        if side == 'BUY':
            return self._round_price(b.best_bid, tick)
        return self._round_price(b.best_ask, tick)

    def _order_size(self, quote_index: int, level: int) -> float:
        return 1.0

    def run_once(self) -> dict:
        summary = super().run_once()
        if isinstance(summary, dict):
            summary['shallow_touch_filter'] = dict(self.shallow_stats)
            self._persist_summary(summary)
        return summary


def run_shallow_touch_shadow_cycle() -> dict:
    maker = ShallowTouchPaperMaker().run_once()
    execution = run_execution_audit()
    queue = run_queue_audit()
    shadow = run_live_shadow_audit()
    return {
        'mode': 'V9_3_SHALLOW_TOUCH_SHADOW_CYCLE',
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
            'shallow_touch_filter': maker.get('shallow_touch_filter'),
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
            'quote_queue_ok_rate': queue.get('quote_queue_ok_rate'),
            'quote_queue_watch_rate': queue.get('quote_queue_watch_rate'),
            'quote_queue_risk_rate': queue.get('quote_queue_risk_rate'),
            'fill_queue_ok_rate': queue.get('fill_queue_ok_rate'),
            'fill_queue_watch_rate': queue.get('fill_queue_watch_rate'),
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
        'interpretation': 'V9.3 keeps only shallow-touch books. If this cannot improve queue risk, the current market universe is not suitable for passive paper realism.',
    }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_shallow_touch_shadow_cycle(), ensure_ascii=False, indent=2, default=str))
