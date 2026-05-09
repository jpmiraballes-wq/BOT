from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from config import settings
from paper_maker_engine import BookTop, MakerConfig, PaperMakerEngine, _float, _now_iso
from paper_maker_markout import run_markout_audit

log = logging.getLogger('paper_maker_god')


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


class GodModePaperMaker(PaperMakerEngine):
    """Strict paper maker wrapper focused on executable markout quality.

    The base simulator is allowed to create lots of quotes. God mode owns the
    fill model, because strict executable markout needs small fills that already
    have an exit-side cushion. Do not delegate accepted strict fills back to the
    base fill model; the base model uses a different near-touch definition and
    can reject already-approved strict fills.
    """

    def __init__(self, cfg: MakerConfig | None = None) -> None:
        super().__init__(cfg)
        self.strict_mode = _bool_env('PAPER_MAKER_GOD_MODE', True)
        self.max_executable_spread = _float_env('PAPER_MAKER_MAX_EXECUTABLE_SPREAD', 0.025)
        self.min_best_bid = _float_env('PAPER_MAKER_MIN_BEST_BID', 0.025)
        self.min_best_ask = _float_env('PAPER_MAKER_MIN_BEST_ASK', 0.035)
        self.min_liquidity = _float_env('PAPER_MAKER_MIN_LIQUIDITY', 5000.0)
        self.reject_high_yes_no_extreme = _bool_env('PAPER_MAKER_REJECT_EXTREME_99C', True)
        self.adverse_token_limit = _int_env('PAPER_MAKER_ADVERSE_TOKEN_LIMIT', 50)
        self.adverse_positive_rate_max = _float_env('PAPER_MAKER_ADVERSE_POSITIVE_RATE_MAX', 0.60)
        self.adverse_loss_min_usd = _float_env('PAPER_MAKER_ADVERSE_LOSS_MIN_USD', 0.25)
        # V6.1: keep toxic fills dead, but allow a measurable sample of small strict fills.
        self.strict_fill_threshold_near = _float_env('PAPER_MAKER_STRICT_FILL_THRESHOLD_NEAR', 0.12)
        self.strict_fill_threshold_far = _float_env('PAPER_MAKER_STRICT_FILL_THRESHOLD_FAR', 0.0)
        self.min_exit_edge_ticks = _float_env('PAPER_MAKER_MIN_EXIT_EDGE_TICKS', 0.002)
        self.strict_max_order_size_usd = _float_env('PAPER_MAKER_STRICT_MAX_ORDER_SIZE_USD', 10.0)
        self.strict_level0_order_size_usd = _float_env('PAPER_MAKER_STRICT_LEVEL0_ORDER_SIZE_USD', 5.0)
        self.filtered_stats: dict[str, int] = {}
        self.blocked_token_shorts: set[str] = set()
        self.strict_fill_rejects: dict[str, int] = {}

    def _load_latest_markout(self) -> dict[str, Any]:
        path = Path(settings.data_dir) / 'paper_maker_markout.json'
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _blocked_tokens_from_markout(self) -> set[str]:
        audit = self._load_latest_markout()
        blocked: set[str] = set()
        for row in (audit.get('top_adverse_tokens') or [])[: self.adverse_token_limit]:
            if not isinstance(row, dict):
                continue
            token = str(row.get('token') or '').strip()
            exec_loss = _float(row.get('executable_markout_usd'))
            positive_rate = _float(row.get('positive_rate'))
            if token and exec_loss <= -abs(self.adverse_loss_min_usd) and positive_rate <= self.adverse_positive_rate_max:
                blocked.add(token)
        return blocked

    def _quality_reason(self, b: BookTop) -> str | None:
        short = b.token_id[:10]
        if short in self.blocked_token_shorts:
            return 'blocked_previous_strict_markout_adverse_token'
        if b.liquidity < self.min_liquidity:
            return 'liquidity_too_low_for_god_mode'
        if b.best_bid < self.min_best_bid:
            return 'penny_trap_best_bid_too_low'
        if b.best_ask < self.min_best_ask:
            return 'penny_trap_best_ask_too_low'
        if self.reject_high_yes_no_extreme and (b.best_bid >= 0.975 or b.best_ask >= 0.985):
            return 'extreme_99c_tail_risk'
        if b.spread > self.max_executable_spread:
            return 'spread_too_wide_for_executable_markout'
        return None

    def discover_books(self) -> list[BookTop]:
        raw_books = super().discover_books()
        if not self.strict_mode:
            return raw_books
        self.blocked_token_shorts = self._blocked_tokens_from_markout()
        kept: list[BookTop] = []
        stats: dict[str, int] = {
            'raw_candidates': len(raw_books),
            'kept': 0,
            'blocked_previous_strict_markout_adverse_token': 0,
            'liquidity_too_low_for_god_mode': 0,
            'penny_trap_best_bid_too_low': 0,
            'penny_trap_best_ask_too_low': 0,
            'extreme_99c_tail_risk': 0,
            'spread_too_wide_for_executable_markout': 0,
        }
        for b in raw_books:
            reason = self._quality_reason(b)
            if reason:
                stats[reason] = stats.get(reason, 0) + 1
                continue
            kept.append(b)
        stats['kept'] = len(kept)
        self.filtered_stats = stats
        log.info('paper_maker_god_filter %s', stats)
        return kept

    def _order_size(self, quote_index: int, level: int) -> float:
        if not self.strict_mode:
            return super()._order_size(quote_index, level)
        if level == 0:
            return min(self.strict_level0_order_size_usd, self.strict_max_order_size_usd)
        return min(self.strict_max_order_size_usd, max(1.0, 2.0 + level * 0.75))

    def _reject_fill(self, reason: str):
        self.strict_fill_rejects[reason] = self.strict_fill_rejects.get(reason, 0) + 1

    def _strict_fill_fraction(self, unit: float, threshold: float) -> float:
        if threshold <= 0:
            return 0.0
        # Small partial fills only. This keeps notional realistic and prevents one
        # bad synthetic fill from dominating the sample.
        return min(0.45, max(0.08, 0.08 + (unit / threshold) * 0.22))

    def _apply_strict_fill(self, q: dict, b: BookTop, inv: dict, avg_cost: dict, cash: float, unit: float, threshold: float):
        side = q['side']
        price = _float(q['price'])
        shares = _float(q.get('shares'))
        size_usd = min(_float(q.get('size_usd')) or shares * price, self.strict_max_order_size_usd)
        max_shares = size_usd / max(price, 0.01)
        fill_shares = min(shares, max_shares) * self._strict_fill_fraction(unit, threshold)
        if fill_shares <= 0:
            return False, cash, 0.0, None

        pnl = 0.0
        token = q['token_id']
        if side == 'BUY':
            cost = fill_shares * price
            if cash < cost:
                self._reject_fill('reject_insufficient_paper_cash')
                return False, cash, 0.0, None
            old_shares = _float(inv.get(token))
            old_avg = _float(avg_cost.get(token))
            new_shares = old_shares + fill_shares
            avg_cost[token] = ((old_shares * old_avg) + cost) / new_shares if new_shares > 0 else 0.0
            inv[token] = new_shares
            cash -= cost
        else:
            have = _float(inv.get(token))
            fill_shares = min(have, fill_shares)
            if fill_shares <= 0:
                self._reject_fill('reject_no_inventory_to_sell')
                return False, cash, 0.0, None
            proceeds = fill_shares * price
            pnl = proceeds - (fill_shares * _float(avg_cost.get(token)))
            inv[token] = max(0.0, have - fill_shares)
            cash += proceeds

        fill = {
            'filled_at': _now_iso(),
            'quote_id': q['quote_id'],
            'token_id': token,
            'token_id_short': token[:10],
            'market_id': q['market_id'],
            'side': side,
            'price': round(price, 6),
            'shares': round(fill_shares, 6),
            'notional_usd': round(fill_shares * price, 6),
            'realized_pnl_usd': round(pnl, 6),
            'paper_only': True,
            'strict_god_fill': True,
            'strict_fill_model': 'v6_1_direct',
        }
        return True, cash, pnl, fill

    def _try_virtual_fill(self, q: dict, b: BookTop, inv: dict, avg_cost: dict, cash: float):
        if not self.strict_mode:
            return super()._try_virtual_fill(q, b, inv, avg_cost, cash)

        reason = self._quality_reason(b)
        if reason:
            self._reject_fill(reason)
            return False, cash, 0.0, None

        side = q['side']
        price = _float(q['price'])
        size_usd = _float(q.get('size_usd')) or (_float(q.get('shares')) * price)
        if size_usd > self.strict_max_order_size_usd:
            self._reject_fill('reject_strict_order_size_too_large')
            return False, cash, 0.0, None

        if side == 'BUY':
            executable_exit_edge = b.best_bid - price
            near_touch = price <= b.best_bid
        else:
            executable_exit_edge = price - b.best_ask
            near_touch = price >= b.best_ask

        if executable_exit_edge < self.min_exit_edge_ticks:
            self._reject_fill('reject_no_immediate_executable_exit_edge')
            return False, cash, 0.0, None
        if not near_touch:
            self._reject_fill('reject_far_from_executable_touch')
            return False, cash, 0.0, None

        unit = self._stable_unit(f"STRICT6_1:{q['quote_id']}:{b.best_bid}:{b.best_ask}:{b.spread}:{price}:{size_usd}")
        threshold = self.strict_fill_threshold_near
        if b.spread > 0.015:
            threshold *= 0.50
        if b.best_bid < 0.06 or b.best_ask > 0.94:
            threshold *= 0.35
        if unit > threshold:
            self._reject_fill('reject_fill_probability')
            return False, cash, 0.0, None

        return self._apply_strict_fill(q, b, inv, avg_cost, cash, unit, threshold)

    def run_once(self) -> dict:
        summary = super().run_once()
        if isinstance(summary, dict):
            summary['god_mode_enabled'] = bool(self.strict_mode)
            summary['strict_quality_filter'] = dict(self.filtered_stats)
            summary['strict_fill_rejects'] = dict(self.strict_fill_rejects)
            summary['blocked_adverse_token_shorts'] = sorted(self.blocked_token_shorts)[:50]
            summary['strict_v6_controls'] = {
                'fill_model': 'v6_1_direct_strict_fill_no_base_recheck',
                'max_executable_spread': self.max_executable_spread,
                'min_liquidity': self.min_liquidity,
                'min_exit_edge_ticks': self.min_exit_edge_ticks,
                'strict_max_order_size_usd': self.strict_max_order_size_usd,
                'strict_fill_threshold_near': self.strict_fill_threshold_near,
                'strict_fill_threshold_far': self.strict_fill_threshold_far,
            }
            self._persist_summary(summary)
        try:
            markout = run_markout_audit()
            if isinstance(markout, dict):
                summary['strict_markout_verdict'] = markout.get('verdict')
                summary['strict_markout_score'] = markout.get('markout_score')
                summary['strict_executable_markout_usd'] = markout.get('executable_markout_usd')
                summary['strict_positive_executable_markout_rate'] = markout.get('positive_executable_markout_rate')
                log.info(
                    'paper_maker_markout_after_cycle verdict=%s measured_fills=%s executable=%s positive_rate=%s',
                    markout.get('verdict'), markout.get('measured_fills'),
                    markout.get('executable_markout_usd'), markout.get('positive_executable_markout_rate'),
                )
                self._persist_summary(summary)
        except Exception as exc:
            log.warning('paper_maker_markout_after_cycle_failed err=%s', exc)
        return summary


def run_paper_maker_once() -> dict:
    return GodModePaperMaker().run_once()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_paper_maker_once(), ensure_ascii=False, indent=2, default=str))
