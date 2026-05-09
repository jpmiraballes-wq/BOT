from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from config import settings
from paper_maker_engine import BookTop, MakerConfig, PaperMakerEngine, _float
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

    V0 produced high throughput, but strict markout showed midpoint-positive /
    executable-negative behavior. This wrapper keeps the high-frequency CLOB
    simulation, but filters the worst adverse tokens and penny traps before quote
    generation. It also runs strict markout after every cycle so the next cycle
    learns from the previous one.
    """

    def __init__(self, cfg: MakerConfig | None = None) -> None:
        super().__init__(cfg)
        self.strict_mode = _bool_env('PAPER_MAKER_GOD_MODE', True)
        self.max_executable_spread = _float_env('PAPER_MAKER_MAX_EXECUTABLE_SPREAD', 0.08)
        self.min_best_bid = _float_env('PAPER_MAKER_MIN_BEST_BID', 0.005)
        self.min_best_ask = _float_env('PAPER_MAKER_MIN_BEST_ASK', 0.012)
        self.adverse_token_limit = _int_env('PAPER_MAKER_ADVERSE_TOKEN_LIMIT', 20)
        self.adverse_positive_rate_max = _float_env('PAPER_MAKER_ADVERSE_POSITIVE_RATE_MAX', 0.35)
        self.adverse_loss_min_usd = _float_env('PAPER_MAKER_ADVERSE_LOSS_MIN_USD', 1.0)
        self.strict_fill_threshold_near = _float_env('PAPER_MAKER_STRICT_FILL_THRESHOLD_NEAR', 0.075)
        self.strict_fill_threshold_far = _float_env('PAPER_MAKER_STRICT_FILL_THRESHOLD_FAR', 0.012)
        self.filtered_stats: dict[str, int] = {}
        self.blocked_token_shorts: set[str] = set()

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
        if b.best_bid < self.min_best_bid:
            return 'penny_trap_best_bid_too_low'
        if b.best_ask < self.min_best_ask:
            return 'penny_trap_best_ask_too_low'
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
            'penny_trap_best_bid_too_low': 0,
            'penny_trap_best_ask_too_low': 0,
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
        # If the filter becomes too strict, keep a small safe fallback instead of
        # producing zero orders. The fallback still excludes known adverse tokens.
        if len(kept) < 8:
            fallback = [b for b in raw_books if b.token_id[:10] not in self.blocked_token_shorts and b.best_bid >= 0.002 and b.spread <= 0.12]
            kept = fallback[: max(8, len(kept))]
            self.filtered_stats['fallback_used'] = 1
            self.filtered_stats['kept'] = len(kept)
        return kept

    def _try_virtual_fill(self, q: dict, b: BookTop, inv: dict, avg_cost: dict, cash: float):
        if self.strict_mode:
            reason = self._quality_reason(b)
            if reason:
                return False, cash, 0.0, None
            # In strict mode, reduce optimistic fills. Wide books and penny names
            # should not generate easy fills just because midpoint looks good.
            side = q['side']
            price = _float(q['price'])
            near_touch = (side == 'BUY' and price >= b.best_bid) or (side == 'SELL' and price <= b.best_ask)
            unit = self._stable_unit(f"STRICT:{q['quote_id']}:{b.best_bid}:{b.best_ask}")
            threshold = self.strict_fill_threshold_near if near_touch else self.strict_fill_threshold_far
            if b.spread > 0.04:
                threshold *= 0.50
            if unit > threshold:
                return False, cash, 0.0, None
        return super()._try_virtual_fill(q, b, inv, avg_cost, cash)

    def run_once(self) -> dict:
        summary = super().run_once()
        if isinstance(summary, dict):
            summary['god_mode_enabled'] = bool(self.strict_mode)
            summary['strict_quality_filter'] = dict(self.filtered_stats)
            summary['blocked_adverse_token_shorts'] = sorted(self.blocked_token_shorts)[:50]
            # Rewrite summary with the quality metadata added.
            self._persist_summary(summary)
        # Run markout after the cycle. It may fail if too fresh; never block maker.
        try:
            markout = run_markout_audit()
            if isinstance(markout, dict):
                summary['strict_markout_verdict'] = markout.get('verdict')
                summary['strict_markout_score'] = markout.get('markout_score')
                summary['strict_executable_markout_usd'] = markout.get('executable_markout_usd')
                self._persist_summary(summary)
        except Exception as exc:
            log.warning('paper_maker_markout_after_cycle_failed err=%s', exc)
        return summary


def run_paper_maker_once() -> dict:
    return GodModePaperMaker().run_once()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_paper_maker_once(), ensure_ascii=False, indent=2, default=str))
