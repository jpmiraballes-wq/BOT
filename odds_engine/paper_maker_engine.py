from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from base44_client import base44
from config import settings
from polymarket_client import PolymarketPublicClient

log = logging.getLogger('paper_maker')


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


@dataclass
class MakerConfig:
    enabled: bool = _bool_env('PAPER_MAKER_ENABLED', True)
    paper_bankroll_usd: float = _float_env('PAPER_MAKER_BANKROLL_USD', 1_000_000.0)
    target_orders_per_day: int = _int_env('PAPER_MAKER_TARGET_ORDERS_PER_DAY', 5_000)
    min_orders_per_cycle: int = _int_env('PAPER_MAKER_MIN_ORDERS_PER_CYCLE', 1_000)
    max_markets: int = _int_env('PAPER_MAKER_MAX_MARKETS', 40)
    book_fetch_limit: int = _int_env('PAPER_MAKER_BOOK_FETCH_LIMIT', 80)
    market_fetch_limit: int = _int_env('PAPER_MAKER_MARKET_FETCH_LIMIT', 150)
    book_timeout_seconds: float = _float_env('PAPER_MAKER_BOOK_TIMEOUT_SECONDS', 2.0)
    quote_levels: int = _int_env('PAPER_MAKER_QUOTE_LEVELS', 6)
    micro_cycles: int = _int_env('PAPER_MAKER_MICRO_CYCLES', 4)
    max_order_size_usd: float = _float_env('PAPER_MAKER_MAX_ORDER_SIZE_USD', 250.0)
    min_order_size_usd: float = _float_env('PAPER_MAKER_MIN_ORDER_SIZE_USD', 1.0)
    small_order_size_usd: float = _float_env('PAPER_MAKER_SMALL_ORDER_SIZE_USD', 3.0)
    block_order_size_usd: float = _float_env('PAPER_MAKER_BLOCK_ORDER_SIZE_USD', 1_000.0)
    block_every_n_quotes: int = _int_env('PAPER_MAKER_BLOCK_EVERY_N_QUOTES', 57)
    max_inventory_per_token_usd: float = _float_env('PAPER_MAKER_MAX_INVENTORY_PER_TOKEN_USD', 10_000.0)
    max_total_inventory_usd: float = _float_env('PAPER_MAKER_MAX_TOTAL_INVENTORY_USD', 100_000.0)
    min_spread: float = _float_env('PAPER_MAKER_MIN_SPREAD', 0.002)
    max_spread: float = _float_env('PAPER_MAKER_MAX_SPREAD', 0.20)
    quote_margin_ticks: int = _int_env('PAPER_MAKER_QUOTE_MARGIN_TICKS', 1)
    live_orders_enabled: bool = False


@dataclass
class BookTop:
    token_id: str
    market_id: str
    question: str
    outcome: str
    best_bid: float
    best_ask: float
    midpoint: float
    spread: float
    last_trade_price: float
    min_order_size: float
    tick_size: float
    liquidity: float
    end_date: str | None
    reward_eligible: bool
    min_incentive_size: float
    max_incentive_spread: float


class PaperMakerEngine:
    """Paper-only high-frequency CLOB maker simulator.

    It never places live orders. It uses real Polymarket markets/order books and
    simulates the behavior pattern seen in top wallets: many small maker quotes,
    frequent cancel/replace, occasional larger blocks, both-side inventory
    management, virtual fills, spread capture, and Base44 snapshots.
    """

    def __init__(self, cfg: MakerConfig | None = None) -> None:
        self.cfg = cfg or MakerConfig()
        self.poly = PolymarketPublicClient(settings)
        self.clob_url = settings.polymarket_clob_url.rstrip('/')
        self.data_dir = Path(settings.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / 'paper_maker_state.json'
        self.summary_path = self.data_dir / 'paper_maker_summary.json'
        self.runs_path = self.data_dir / 'paper_maker_runs.jsonl'
        self.quotes_path = self.data_dir / 'paper_maker_quotes.jsonl'
        self.fills_path = self.data_dir / 'paper_maker_fills.jsonl'

    def _initial_state(self) -> dict:
        return {
            'version': 2,
            'created_at': _now_iso(),
            'cash_usd': self.cfg.paper_bankroll_usd,
            'bankroll_usd': self.cfg.paper_bankroll_usd,
            'inventory': {},
            'avg_cost': {},
            'open_quotes': {},
            'realized_spread_pnl_usd': 0.0,
            'day': datetime.now(timezone.utc).date().isoformat(),
            'totals': {'orders_simulated': 0, 'cancels_simulated': 0, 'fills_simulated': 0, 'requotes_simulated': 0},
            'day_totals': {'orders_simulated': 0, 'cancels_simulated': 0, 'fills_simulated': 0, 'requotes_simulated': 0},
        }

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return self._initial_state()
        try:
            state = json.loads(self.state_path.read_text())
        except Exception:
            return self._initial_state()
        today = datetime.now(timezone.utc).date().isoformat()
        if state.get('day') != today:
            state['day'] = today
            state['day_totals'] = {'orders_simulated': 0, 'cancels_simulated': 0, 'fills_simulated': 0, 'requotes_simulated': 0}
        state.setdefault('inventory', {})
        state.setdefault('avg_cost', {})
        state.setdefault('open_quotes', {})
        state.setdefault('realized_spread_pnl_usd', 0.0)
        state.setdefault('totals', {'orders_simulated': 0, 'cancels_simulated': 0, 'fills_simulated': 0, 'requotes_simulated': 0})
        state.setdefault('day_totals', {'orders_simulated': 0, 'cancels_simulated': 0, 'fills_simulated': 0, 'requotes_simulated': 0})
        return state

    def _save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        tmp.replace(self.state_path)

    def _fetch_book(self, token_id: str) -> dict | None:
        try:
            resp = requests.get(f'{self.clob_url}/book', params={'token_id': token_id}, timeout=self.cfg.book_timeout_seconds)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception as exc:
            log.debug('book fetch exception token=%s err=%s', token_id[:10], exc)
            return None

    def _best_levels(self, book: dict) -> tuple[float, float, float, float, float, float]:
        bids = book.get('bids') or []
        asks = book.get('asks') or []
        best_bid = max([_float(x.get('price')) for x in bids if isinstance(x, dict)] or [0.0])
        best_ask = min([_float(x.get('price'), 1.0) for x in asks if isinstance(x, dict)] or [0.0])
        last = _float(book.get('last_trade_price'))
        if best_ask <= 0 and last > 0:
            best_ask = min(0.99, last + 0.01)
        if best_bid <= 0 and last > 0:
            best_bid = max(0.01, last - 0.01)
        midpoint = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > best_bid else last
        spread = (best_ask - best_bid) if best_bid > 0 and best_ask > best_bid else 0.0
        tick = _float(book.get('tick_size'), 0.01) or 0.01
        return best_bid, best_ask, midpoint, spread, last or midpoint, tick

    def _reward_fields(self, raw: dict) -> tuple[bool, float, float]:
        min_size = _float(raw.get('min_incentive_size') or raw.get('minIncentiveSize') or raw.get('minimum_order_size'))
        max_spread = _float(raw.get('max_incentive_spread') or raw.get('maxIncentiveSpread'))
        rewards = raw.get('rewards') or raw.get('reward') or raw.get('rewardsDaily') or raw.get('liquidityRewards')
        eligible = bool(rewards or min_size or max_spread)
        return eligible, min_size, max_spread

    def discover_books(self) -> list[BookTop]:
        cfg = self.cfg
        markets = self.poly.fetch_active_markets(limit=cfg.market_fetch_limit, offset=0, search=None)
        candidates: list[BookTop] = []
        attempted_books = 0
        target_candidates = max(1, cfg.max_markets)
        max_attempts = max(target_candidates * 2, cfg.book_fetch_limit)
        for m in markets:
            if attempted_books >= max_attempts or len(candidates) >= target_candidates:
                break
            if not m.token_ids:
                continue
            raw = m.raw_payload or {}
            reward_eligible, min_inc_size, max_inc_spread = self._reward_fields(raw)
            for i, token_id in enumerate(m.token_ids[:2]):
                if attempted_books >= max_attempts or len(candidates) >= target_candidates:
                    break
                if not token_id:
                    continue
                attempted_books += 1
                if attempted_books % 20 == 0:
                    log.info('paper_maker_discovery_progress attempted_books=%s candidates=%s', attempted_books, len(candidates))
                book = self._fetch_book(str(token_id))
                if not book:
                    continue
                bid, ask, mid, spread, last, tick = self._best_levels(book)
                if mid <= 0 or spread <= 0:
                    continue
                if spread < cfg.min_spread or spread > cfg.max_spread:
                    continue
                outcome = m.outcomes[i] if i < len(m.outcomes) else ('YES' if i == 0 else 'NO')
                candidates.append(BookTop(
                    token_id=str(token_id), market_id=str(m.id), question=m.question, outcome=str(outcome),
                    best_bid=bid, best_ask=ask, midpoint=mid, spread=spread, last_trade_price=last,
                    min_order_size=_float(book.get('min_order_size'), 1.0) or 1.0,
                    tick_size=tick, liquidity=float(m.liquidity or 0.0), end_date=m.end_date,
                    reward_eligible=reward_eligible, min_incentive_size=min_inc_size, max_incentive_spread=max_inc_spread,
                ))
        candidates.sort(key=lambda x: (1 if x.reward_eligible else 0, x.liquidity, x.spread), reverse=True)
        log.info('paper_maker_discovery_done markets=%s attempted_books=%s candidates=%s', len(markets), attempted_books, len(candidates))
        return candidates[:target_candidates]

    def _round_price(self, price: float, tick: float) -> float:
        tick = tick if tick > 0 else 0.01
        return min(0.99, max(0.01, round(round(price / tick) * tick, 4)))

    def _stable_unit(self, key: str) -> float:
        h = hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]
        return int(h, 16) / 0xFFFFFFFF

    def _quote_price(self, b: BookTop, side: str, level: int, micro: int) -> float:
        tick = b.tick_size or 0.01
        wobble = (micro % 3) * tick
        offset = (self.cfg.quote_margin_ticks + level) * tick + wobble
        if side == 'BUY':
            px = min(b.best_bid + tick + wobble, b.midpoint - offset)
            return self._round_price(min(px, b.best_ask - tick), tick)
        px = max(b.best_ask - tick - wobble, b.midpoint + offset)
        return self._round_price(max(px, b.best_bid + tick), tick)

    def _order_size(self, quote_index: int, level: int) -> float:
        if self.cfg.block_every_n_quotes > 0 and quote_index % self.cfg.block_every_n_quotes == 0:
            return min(self.cfg.block_order_size_usd, self.cfg.max_order_size_usd * 8)
        if level == 0:
            return min(self.cfg.max_order_size_usd, max(self.cfg.small_order_size_usd, self.cfg.min_order_size_usd * 5))
        return min(self.cfg.max_order_size_usd, max(self.cfg.min_order_size_usd, self.cfg.small_order_size_usd * (1 + level * 0.35)))

    def _append_jsonl(self, path: Path, rows: list[dict]) -> None:
        if not rows:
            return
        with path.open('a', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')

    def _try_virtual_fill(self, q: dict, b: BookTop, inv: dict, avg_cost: dict, cash: float) -> tuple[bool, float, float, dict | None]:
        side = q['side']
        price = _float(q['price'])
        shares = _float(q['shares'])
        near_touch = (side == 'BUY' and price >= b.best_bid) or (side == 'SELL' and price <= b.best_ask)
        unit = self._stable_unit(f"{q['quote_id']}:{b.best_bid}:{b.best_ask}")
        fill_threshold = 0.18 if near_touch else 0.035
        if unit > fill_threshold:
            return False, cash, 0.0, None
        fill_fraction = 0.15 + (unit / max(fill_threshold, 0.001)) * 0.65
        fill_shares = max(0.0, shares * min(0.8, fill_fraction))
        if fill_shares <= 0:
            return False, cash, 0.0, None
        pnl = 0.0
        token = q['token_id']
        if side == 'BUY':
            cost = fill_shares * price
            if cash < cost:
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
                return False, cash, 0.0, None
            proceeds = fill_shares * price
            pnl = proceeds - (fill_shares * _float(avg_cost.get(token)))
            inv[token] = max(0.0, have - fill_shares)
            cash += proceeds
        fill = {
            'filled_at': _now_iso(), 'quote_id': q['quote_id'], 'token_id': token, 'token_id_short': token[:10],
            'market_id': q['market_id'], 'side': side, 'price': price, 'shares': round(fill_shares, 6),
            'notional_usd': round(fill_shares * price, 6), 'realized_pnl_usd': round(pnl, 6),
            'paper_only': True,
        }
        return True, cash, pnl, fill

    def run_once(self) -> dict:
        started = time.time()
        cfg = self.cfg
        state = self._load_state()
        if not cfg.enabled:
            return {'mode': 'PAPER_MAKER', 'enabled': False, 'reason': 'PAPER_MAKER_ENABLED=false'}

        books = self.discover_books()
        if not books:
            summary = {
                'mode': 'PAPER_MAKER', 'enabled': True, 'live_orders_enabled': False,
                'generated_at': _now_iso(), 'runtime_seconds': round(time.time() - started, 3),
                'paper_bankroll_usd': cfg.paper_bankroll_usd, 'markets_scanned': 0, 'markets_quoted': 0,
                'orders_simulated_this_cycle': 0, 'cancels_simulated_this_cycle': 0,
                'fills_simulated_this_cycle': 0, 'orders_simulated_today': int(state.get('day_totals', {}).get('orders_simulated', 0)),
                'target_orders_per_day': cfg.target_orders_per_day, 'target_progress_pct': 0,
                'maker_total_pnl_usd': 0, 'inventory_exposure_usd': 0,
                'error': 'no_orderbooks_available_for_paper_maker',
            }
            self._persist_summary(summary)
            return summary

        inv = state.setdefault('inventory', {})
        avg_cost = state.setdefault('avg_cost', {})
        open_quotes = state.setdefault('open_quotes', {})
        cash = float(state.get('cash_usd', cfg.paper_bankroll_usd))

        orders = cancels = fills = requotes = 0
        spread_captured = 0.0
        quote_rows: list[dict] = []
        fill_rows: list[dict] = []

        if open_quotes:
            cancels += len(open_quotes)
            requotes += len(open_quotes)
            open_quotes.clear()

        quote_index = 0
        per_token_order_counts: dict[str, int] = {}

        for micro in range(max(1, cfg.micro_cycles)):
            for b in books:
                current_total_inventory = sum(_float(inv.get(x.token_id)) * x.midpoint for x in books)
                current_inv_usd = _float(inv.get(b.token_id)) * b.midpoint
                if current_total_inventory >= cfg.max_total_inventory_usd:
                    continue
                for level in range(max(1, cfg.quote_levels)):
                    sides = ['BUY']
                    if _float(inv.get(b.token_id)) > 0 or (level <= 1 and quote_index % 3 == 0):
                        sides.append('SELL')
                    for side in sides:
                        if side == 'BUY' and current_inv_usd >= cfg.max_inventory_per_token_usd:
                            continue
                        price = self._quote_price(b, side, level, micro)
                        if price <= 0 or price >= 1:
                            continue
                        size_usd = self._order_size(quote_index + 1, level)
                        if side == 'SELL':
                            have = _float(inv.get(b.token_id))
                            if have <= 0:
                                size_usd = min(size_usd, cfg.small_order_size_usd)
                            else:
                                size_usd = min(size_usd, have * price)
                        shares = size_usd / max(price, 0.01)
                        quote_index += 1
                        qid = f"{b.token_id}:{side}:{micro}:{level}:{quote_index}:{int(time.time())}"
                        q = {
                            'quote_id': qid, 'created_at': _now_iso(), 'token_id': b.token_id, 'token_id_short': b.token_id[:10],
                            'market_id': b.market_id, 'question': b.question[:160], 'outcome': b.outcome,
                            'side': side, 'price': price, 'shares': round(shares, 6), 'size_usd': round(size_usd, 6),
                            'level': level, 'micro_cycle': micro, 'paper_only': True,
                        }
                        open_quotes[qid] = q
                        quote_rows.append(q)
                        orders += 1
                        per_token_order_counts[b.token_id] = per_token_order_counts.get(b.token_id, 0) + 1
                        did_fill, cash, pnl, fill = self._try_virtual_fill(q, b, inv, avg_cost, cash)
                        if did_fill:
                            fills += 1
                            spread_captured += pnl
                            if fill:
                                fill_rows.append(fill)
                            open_quotes.pop(qid, None)

        churn_books = books[: max(1, min(len(books), 20))]
        while orders < cfg.min_orders_per_cycle and churn_books:
            b = churn_books[orders % len(churn_books)]
            side = 'BUY' if orders % 2 == 0 else 'SELL'
            level = orders % max(1, cfg.quote_levels)
            price = self._quote_price(b, side, level, orders % max(1, cfg.micro_cycles))
            size_usd = min(cfg.small_order_size_usd, cfg.max_order_size_usd)
            shares = size_usd / max(price, 0.01)
            qid = f"{b.token_id}:{side}:CHURN:{orders}:{int(time.time())}"
            q = {
                'quote_id': qid, 'created_at': _now_iso(), 'token_id': b.token_id, 'token_id_short': b.token_id[:10],
                'market_id': b.market_id, 'question': b.question[:160], 'outcome': b.outcome,
                'side': side, 'price': price, 'shares': round(shares, 6), 'size_usd': round(size_usd, 6),
                'level': level, 'micro_cycle': 'CHURN', 'paper_only': True,
            }
            quote_rows.append(q)
            orders += 1
            cancels += 1
            requotes += 1
            per_token_order_counts[b.token_id] = per_token_order_counts.get(b.token_id, 0) + 1

        market_rows = []
        inventory_usd = 0.0
        unrealized_pnl = 0.0
        for b in books:
            shares = _float(inv.get(b.token_id))
            token_inventory_usd = shares * b.midpoint
            inventory_usd += token_inventory_usd
            unrealized_pnl += shares * (b.midpoint - _float(avg_cost.get(b.token_id)))
            bid_quote = self._quote_price(b, 'BUY', 0, 0)
            ask_quote = self._quote_price(b, 'SELL', 0, 0)
            market_rows.append({
                'market_id': b.market_id, 'token_id_short': b.token_id[:10], 'token_id': b.token_id,
                'question': b.question[:180], 'outcome': b.outcome, 'best_bid': b.best_bid, 'best_ask': b.best_ask,
                'midpoint': b.midpoint, 'spread': b.spread, 'liquidity': b.liquidity,
                'bid_quote': bid_quote, 'ask_quote': ask_quote,
                'orders_placed_this_cycle': per_token_order_counts.get(b.token_id, 0),
                'inventory_shares': round(shares, 6), 'inventory_usd': round(token_inventory_usd, 6),
                'reward_eligible': b.reward_eligible, 'min_incentive_size': b.min_incentive_size,
                'max_incentive_spread': b.max_incentive_spread,
                'risk_status': 'inventory_limit' if token_inventory_usd >= cfg.max_inventory_per_token_usd else 'quoting',
            })

        state['cash_usd'] = cash
        state['inventory'] = inv
        state['avg_cost'] = avg_cost
        state['open_quotes'] = open_quotes
        state['realized_spread_pnl_usd'] = _float(state.get('realized_spread_pnl_usd')) + spread_captured
        for key, value in {
            'orders_simulated': orders,
            'cancels_simulated': cancels,
            'fills_simulated': fills,
            'requotes_simulated': requotes,
        }.items():
            state.setdefault('totals', {})[key] = int(state.setdefault('totals', {}).get(key, 0)) + int(value)
            state.setdefault('day_totals', {})[key] = int(state.setdefault('day_totals', {}).get(key, 0)) + int(value)
        self._save_state(state)
        self._append_jsonl(self.quotes_path, quote_rows[-2000:])
        self._append_jsonl(self.fills_path, fill_rows[-2000:])

        estimated_rewards = self._estimate_rewards(market_rows, orders)
        maker_total_pnl = _float(state.get('realized_spread_pnl_usd')) + unrealized_pnl + estimated_rewards
        summary = {
            'mode': 'PAPER_MAKER', 'enabled': True, 'live_orders_enabled': False, 'generated_at': _now_iso(),
            'runtime_seconds': round(time.time() - started, 3), 'paper_bankroll_usd': cfg.paper_bankroll_usd,
            'cash_usd': round(cash, 6), 'markets_scanned': len(books),
            'markets_quoted': len([r for r in market_rows if r['orders_placed_this_cycle'] > 0]),
            'orders_simulated_this_cycle': orders, 'cancels_simulated_this_cycle': cancels,
            'fills_simulated_this_cycle': fills, 'requotes_simulated_this_cycle': requotes,
            'orders_simulated_today': int(state.get('day_totals', {}).get('orders_simulated', 0)),
            'cancels_simulated_today': int(state.get('day_totals', {}).get('cancels_simulated', 0)),
            'fills_simulated_today': int(state.get('day_totals', {}).get('fills_simulated', 0)),
            'target_orders_per_day': cfg.target_orders_per_day,
            'target_progress_pct': round(100.0 * int(state.get('day_totals', {}).get('orders_simulated', 0)) / max(1, cfg.target_orders_per_day), 4),
            'open_quotes': len(open_quotes), 'inventory_tokens': len([k for k, v in inv.items() if _float(v) > 0]),
            'inventory_exposure_usd': round(inventory_usd, 6),
            'realized_spread_pnl_usd': round(_float(state.get('realized_spread_pnl_usd')), 6),
            'unrealized_inventory_pnl_usd': round(unrealized_pnl, 6),
            'maker_total_pnl_usd': round(maker_total_pnl, 6),
            'estimated_rewards_usd': round(estimated_rewards, 6),
            'min_orders_per_cycle': cfg.min_orders_per_cycle,
            'quote_levels': cfg.quote_levels,
            'micro_cycles': cfg.micro_cycles,
            'book_fetch_limit': cfg.book_fetch_limit,
            'market_fetch_limit': cfg.market_fetch_limit,
            'best_markets': market_rows[:20],
            'state_path': str(self.state_path), 'summary_path': str(self.summary_path),
            'paper_only_note': 'High-frequency paper maker simulation. No live orders are sent.',
        }
        self._persist_summary(summary)
        log.info(
            'paper_maker_summary mode=PAPER_MAKER markets=%s orders=%s cancels=%s fills=%s today_orders=%s inventory=%.2f pnl=%.4f rewards=%.4f',
            summary['markets_quoted'], orders, cancels, fills, summary['orders_simulated_today'],
            summary['inventory_exposure_usd'], summary['maker_total_pnl_usd'], summary['estimated_rewards_usd'],
        )
        return summary

    def _estimate_rewards(self, rows: list[dict], orders: int) -> float:
        eligible = [r for r in rows if r.get('reward_eligible')]
        if not eligible:
            return 0.0
        quality = 0.0
        for r in eligible:
            spread = _float(r.get('spread'))
            max_spread = _float(r.get('max_incentive_spread')) or self.cfg.max_spread
            if spread <= max_spread:
                quality += min(1.0, max_spread / max(spread, 0.001))
        return round(min(250.0, quality * max(1, orders) * 0.0005), 6)

    def _persist_summary(self, summary: dict) -> None:
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        with self.runs_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(summary, ensure_ascii=False, default=str) + '\n')
        if settings.base44_write_enabled:
            base44.post_record('MakerRunSummary', summary)


def run_paper_maker_once() -> dict:
    return PaperMakerEngine().run_once()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_paper_maker_once(), ensure_ascii=False, indent=2, default=str))
