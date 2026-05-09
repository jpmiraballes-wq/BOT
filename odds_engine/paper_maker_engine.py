from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import settings
from polymarket_client import PolymarketPublicClient
from base44_client import base44

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
    max_markets: int = _int_env('PAPER_MAKER_MAX_MARKETS', 40)
    max_order_size_usd: float = _float_env('PAPER_MAKER_MAX_ORDER_SIZE_USD', 250.0)
    min_order_size_usd: float = _float_env('PAPER_MAKER_MIN_ORDER_SIZE_USD', 5.0)
    max_inventory_per_token_usd: float = _float_env('PAPER_MAKER_MAX_INVENTORY_PER_TOKEN_USD', 5_000.0)
    max_total_inventory_usd: float = _float_env('PAPER_MAKER_MAX_TOTAL_INVENTORY_USD', 50_000.0)
    target_orders_per_day: int = _int_env('PAPER_MAKER_TARGET_ORDERS_PER_DAY', 5_000)
    quote_margin: float = _float_env('PAPER_MAKER_QUOTE_MARGIN', 0.005)
    min_spread: float = _float_env('PAPER_MAKER_MIN_SPREAD', 0.005)
    max_spread: float = _float_env('PAPER_MAKER_MAX_SPREAD', 0.12)
    cancel_replace_every_run: bool = _bool_env('PAPER_MAKER_CANCEL_REPLACE_EVERY_RUN', True)
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
    """Paper-only CLOB market-maker simulator.

    This module does NOT place live orders. It reads real Polymarket market data
    and order books, maintains a fake bankroll/inventory, simulates quote / cancel
    / replace, and publishes MakerRunSummary for Base44.
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

    def _initial_state(self) -> dict:
        return {
            'version': 1,
            'created_at': _now_iso(),
            'cash_usd': self.cfg.paper_bankroll_usd,
            'bankroll_usd': self.cfg.paper_bankroll_usd,
            'inventory': {},
            'avg_cost': {},
            'open_quotes': {},
            'realized_pnl_usd': 0.0,
            'totals': {
                'orders_simulated': 0,
                'cancels_simulated': 0,
                'fills_simulated': 0,
                'requotes_simulated': 0,
            },
            'day': datetime.now(timezone.utc).date().isoformat(),
            'day_totals': {
                'orders_simulated': 0,
                'cancels_simulated': 0,
                'fills_simulated': 0,
                'requotes_simulated': 0,
            },
        }

    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return self._initial_state()
        try:
            state = json.loads(self.state_path.read_text())
            today = datetime.now(timezone.utc).date().isoformat()
            if state.get('day') != today:
                state['day'] = today
                state['day_totals'] = {
                    'orders_simulated': 0,
                    'cancels_simulated': 0,
                    'fills_simulated': 0,
                    'requotes_simulated': 0,
                }
            return state
        except Exception:
            return self._initial_state()

    def _save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        tmp.replace(self.state_path)

    def _fetch_book(self, token_id: str) -> dict | None:
        try:
            resp = requests.get(f'{self.clob_url}/book', params={'token_id': token_id}, timeout=8)
            if resp.status_code >= 400:
                log.debug('book fetch failed token=%s status=%s body=%s', token_id[:10], resp.status_code, resp.text[:160])
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
        if best_ask <= 0 and _float(book.get('last_trade_price')) > 0:
            last = _float(book.get('last_trade_price'))
            best_ask = min(0.99, last + 0.01)
        if best_bid <= 0 and _float(book.get('last_trade_price')) > 0:
            last = _float(book.get('last_trade_price'))
            best_bid = max(0.01, last - 0.01)
        midpoint = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > best_bid else _float(book.get('last_trade_price'))
        spread = (best_ask - best_bid) if best_bid > 0 and best_ask > best_bid else 0.0
        last = _float(book.get('last_trade_price'), midpoint)
        tick = _float(book.get('tick_size'), 0.01) or 0.01
        return best_bid, best_ask, midpoint, spread, last, tick

    def _reward_fields(self, raw: dict) -> tuple[bool, float, float]:
        min_size = _float(raw.get('min_incentive_size') or raw.get('minIncentiveSize') or raw.get('minimum_order_size'))
        max_spread = _float(raw.get('max_incentive_spread') or raw.get('maxIncentiveSpread'))
        rewards = raw.get('rewards') or raw.get('reward') or raw.get('rewardsDaily') or raw.get('liquidityRewards')
        eligible = bool(rewards or min_size or max_spread)
        return eligible, min_size, max_spread

    def discover_books(self) -> list[BookTop]:
        markets = self.poly.fetch_active_markets(limit=300, offset=0, search=None)
        candidates = []
        for m in markets:
            if not m.token_ids:
                continue
            raw = m.raw_payload or {}
            reward_eligible, min_inc_size, max_inc_spread = self._reward_fields(raw)
            for i, token_id in enumerate(m.token_ids[:2]):
                if not token_id:
                    continue
                book = self._fetch_book(str(token_id))
                if not book:
                    continue
                bid, ask, mid, spread, last, tick = self._best_levels(book)
                if mid <= 0 or spread <= 0:
                    continue
                if spread < self.cfg.min_spread or spread > self.cfg.max_spread:
                    continue
                outcome = (m.outcomes[i] if i < len(m.outcomes) else ('YES' if i == 0 else 'NO'))
                candidates.append(BookTop(
                    token_id=str(token_id),
                    market_id=str(m.id),
                    question=m.question,
                    outcome=str(outcome),
                    best_bid=bid,
                    best_ask=ask,
                    midpoint=mid,
                    spread=spread,
                    last_trade_price=last,
                    min_order_size=_float(book.get('min_order_size'), 1.0) or 1.0,
                    tick_size=tick,
                    liquidity=float(m.liquidity or 0.0),
                    end_date=m.end_date,
                    reward_eligible=reward_eligible,
                    min_incentive_size=min_inc_size,
                    max_incentive_spread=max_inc_spread,
                ))
        candidates.sort(key=lambda x: (
            1 if x.reward_eligible else 0,
            x.liquidity,
            x.spread,
        ), reverse=True)
        return candidates[: max(1, self.cfg.max_markets)]

    def _round_price(self, price: float, tick: float) -> float:
        if tick <= 0:
            tick = 0.01
        rounded = round(round(price / tick) * tick, 4)
        return min(0.99, max(0.01, rounded))

    def _quote_prices(self, b: BookTop) -> tuple[float, float]:
        # V0: use orderbook midpoint as fair proxy. Later we plug in our sports
        # fair-value engine when mapping confidence exists.
        bid = self._round_price(min(b.best_bid + b.tick_size, b.midpoint - self.cfg.quote_margin), b.tick_size)
        ask = self._round_price(max(b.best_ask - b.tick_size, b.midpoint + self.cfg.quote_margin), b.tick_size)
        if ask <= bid:
            ask = self._round_price(bid + b.tick_size, b.tick_size)
        return bid, ask

    def run_once(self) -> dict:
        started = time.time()
        cfg = self.cfg
        state = self._load_state()
        if not cfg.enabled:
            return {'mode': 'PAPER_MAKER', 'enabled': False, 'reason': 'PAPER_MAKER_ENABLED=false'}

        books = self.discover_books()
        books_by_token = {b.token_id: b for b in books}
        orders = 0
        cancels = 0
        fills = 0
        requotes = 0
        spread_captured = 0.0

        inv = state.setdefault('inventory', {})
        avg_cost = state.setdefault('avg_cost', {})
        open_quotes = state.setdefault('open_quotes', {})
        cash = float(state.get('cash_usd', cfg.paper_bankroll_usd))

        # 1) Simulate fills against previous quotes using current book top.
        for qid, q in list(open_quotes.items()):
            token = str(q.get('token_id') or '')
            b = books_by_token.get(token)
            if not b:
                # stale quote -> cancel
                open_quotes.pop(qid, None)
                cancels += 1
                continue
            side = q.get('side')
            price = _float(q.get('price'))
            shares = _float(q.get('shares'))
            if shares <= 0 or price <= 0:
                open_quotes.pop(qid, None)
                cancels += 1
                continue
            if side == 'BUY' and b.best_ask <= price:
                cost = shares * price
                if cash >= cost:
                    old_shares = _float(inv.get(token))
                    old_avg = _float(avg_cost.get(token))
                    new_shares = old_shares + shares
                    avg_cost[token] = ((old_shares * old_avg) + cost) / new_shares if new_shares > 0 else 0.0
                    inv[token] = new_shares
                    cash -= cost
                    fills += 1
                open_quotes.pop(qid, None)
            elif side == 'SELL' and b.best_bid >= price:
                have = _float(inv.get(token))
                sell_shares = min(have, shares)
                if sell_shares > 0:
                    proceeds = sell_shares * price
                    cost_basis = sell_shares * _float(avg_cost.get(token))
                    spread_captured += proceeds - cost_basis
                    cash += proceeds
                    inv[token] = max(0.0, have - sell_shares)
                    fills += 1
                open_quotes.pop(qid, None)

        # 2) Cancel/replace remaining quotes each cycle to simulate active maker behavior.
        if cfg.cancel_replace_every_run and open_quotes:
            cancels += len(open_quotes)
            requotes += len(open_quotes)
            open_quotes.clear()

        total_inventory_usd = 0.0
        token_mark = {}
        for b in books:
            shares = _float(inv.get(b.token_id))
            token_mark[b.token_id] = b.midpoint
            total_inventory_usd += shares * b.midpoint

        # 3) Place new quotes: buy quotes always; sell quotes only when inventory exists.
        market_rows = []
        for b in books:
            if total_inventory_usd >= cfg.max_total_inventory_usd:
                risk_status = 'max_total_inventory_reached'
            else:
                risk_status = 'quoting'
            bid_price, ask_price = self._quote_prices(b)
            order_size = max(cfg.min_order_size_usd, min(cfg.max_order_size_usd, cfg.paper_bankroll_usd * 0.0005))
            current_inv_usd = _float(inv.get(b.token_id)) * b.midpoint

            placed_for_token = 0
            if risk_status == 'quoting' and current_inv_usd < cfg.max_inventory_per_token_usd:
                shares = order_size / bid_price
                qid = f"{b.token_id}:BUY:{int(time.time())}"
                open_quotes[qid] = {
                    'quote_id': qid,
                    'token_id': b.token_id,
                    'market_id': b.market_id,
                    'side': 'BUY',
                    'price': bid_price,
                    'shares': shares,
                    'size_usd': order_size,
                    'created_at': _now_iso(),
                }
                orders += 1
                placed_for_token += 1

            have = _float(inv.get(b.token_id))
            if have > 0:
                sell_shares = min(have, cfg.max_order_size_usd / max(ask_price, 0.01))
                if sell_shares > 0:
                    qid = f"{b.token_id}:SELL:{int(time.time())}"
                    open_quotes[qid] = {
                        'quote_id': qid,
                        'token_id': b.token_id,
                        'market_id': b.market_id,
                        'side': 'SELL',
                        'price': ask_price,
                        'shares': sell_shares,
                        'size_usd': sell_shares * ask_price,
                        'created_at': _now_iso(),
                    }
                    orders += 1
                    placed_for_token += 1

            market_rows.append({
                'market_id': b.market_id,
                'token_id_short': b.token_id[:10],
                'token_id': b.token_id,
                'question': b.question[:180],
                'outcome': b.outcome,
                'best_bid': b.best_bid,
                'best_ask': b.best_ask,
                'midpoint': b.midpoint,
                'spread': b.spread,
                'liquidity': b.liquidity,
                'bid_quote': bid_price,
                'ask_quote': ask_price,
                'orders_placed_this_cycle': placed_for_token,
                'inventory_shares': _float(inv.get(b.token_id)),
                'inventory_usd': round(_float(inv.get(b.token_id)) * b.midpoint, 6),
                'reward_eligible': b.reward_eligible,
                'min_incentive_size': b.min_incentive_size,
                'max_incentive_spread': b.max_incentive_spread,
                'risk_status': risk_status,
            })

        unrealized_pnl = 0.0
        inventory_usd = 0.0
        for token, shares_raw in inv.items():
            shares = _float(shares_raw)
            mark = token_mark.get(token, _float(avg_cost.get(token)))
            inventory_usd += shares * mark
            unrealized_pnl += shares * (mark - _float(avg_cost.get(token)))

        state['cash_usd'] = cash
        state['inventory'] = inv
        state['avg_cost'] = avg_cost
        state['open_quotes'] = open_quotes
        state['realized_pnl_usd'] = _float(state.get('realized_pnl_usd')) + spread_captured
        for key, value in {
            'orders_simulated': orders,
            'cancels_simulated': cancels,
            'fills_simulated': fills,
            'requotes_simulated': requotes,
        }.items():
            state.setdefault('totals', {})[key] = int(state.setdefault('totals', {}).get(key, 0)) + int(value)
            state.setdefault('day_totals', {})[key] = int(state.setdefault('day_totals', {}).get(key, 0)) + int(value)
        self._save_state(state)

        estimated_rewards = self._estimate_rewards(market_rows)
        summary = {
            'mode': 'PAPER_MAKER',
            'enabled': True,
            'live_orders_enabled': False,
            'generated_at': _now_iso(),
            'runtime_seconds': round(time.time() - started, 3),
            'paper_bankroll_usd': cfg.paper_bankroll_usd,
            'cash_usd': round(cash, 6),
            'markets_scanned': len(books),
            'markets_quoted': len([r for r in market_rows if r['orders_placed_this_cycle'] > 0]),
            'orders_simulated_this_cycle': orders,
            'cancels_simulated_this_cycle': cancels,
            'fills_simulated_this_cycle': fills,
            'requotes_simulated_this_cycle': requotes,
            'orders_simulated_today': int(state.get('day_totals', {}).get('orders_simulated', 0)),
            'cancels_simulated_today': int(state.get('day_totals', {}).get('cancels_simulated', 0)),
            'fills_simulated_today': int(state.get('day_totals', {}).get('fills_simulated', 0)),
            'target_orders_per_day': cfg.target_orders_per_day,
            'target_progress_pct': round(100.0 * int(state.get('day_totals', {}).get('orders_simulated', 0)) / max(1, cfg.target_orders_per_day), 4),
            'open_quotes': len(open_quotes),
            'inventory_tokens': len([k for k, v in inv.items() if _float(v) > 0]),
            'inventory_exposure_usd': round(inventory_usd, 6),
            'realized_spread_pnl_usd': round(_float(state.get('realized_pnl_usd')), 6),
            'unrealized_inventory_pnl_usd': round(unrealized_pnl, 6),
            'maker_total_pnl_usd': round(_float(state.get('realized_pnl_usd')) + unrealized_pnl + estimated_rewards, 6),
            'estimated_rewards_usd': round(estimated_rewards, 6),
            'best_markets': market_rows[:10],
            'state_path': str(self.state_path),
            'summary_path': str(self.summary_path),
        }
        self._persist_summary(summary)
        log.info(
            'paper_maker_summary mode=PAPER_MAKER markets=%s orders=%s cancels=%s fills=%s today_orders=%s inventory=%.2f pnl=%.4f rewards=%.4f',
            summary['markets_quoted'], orders, cancels, fills, summary['orders_simulated_today'],
            summary['inventory_exposure_usd'], summary['maker_total_pnl_usd'], summary['estimated_rewards_usd'],
        )
        return summary

    def _estimate_rewards(self, rows: list[dict]) -> float:
        # Conservative placeholder: rewards are not guaranteed. If Gamma exposes
        # reward eligibility fields, give a tiny paper estimate proportional to
        # quote quality and number of eligible markets. This is explicitly PAPER.
        eligible = [r for r in rows if r.get('reward_eligible')]
        if not eligible:
            return 0.0
        score = 0.0
        for r in eligible:
            spread = _float(r.get('spread'))
            max_spread = _float(r.get('max_incentive_spread')) or self.cfg.max_spread
            if spread <= max_spread:
                score += min(1.0, max_spread / max(spread, 0.001)) * 0.01
        return min(25.0, score)

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
    result = run_paper_maker_once()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
