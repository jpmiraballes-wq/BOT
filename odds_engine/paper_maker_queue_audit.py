from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from base44_client import base44
from config import settings
from paper_maker_engine import _float, _now_iso

log = logging.getLogger('paper_maker_queue_audit')


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


def _parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0.0


def _read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except Exception:
        return []
    rows: list[dict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if isinstance(r, dict):
                rows.append(r)
        except Exception:
            continue
    return rows


def _book_levels(book: dict, side: str) -> list[dict]:
    levels = book.get('bids') if side == 'BUY' else book.get('asks')
    levels = levels or []
    if side == 'BUY':
        return sorted([x for x in levels if isinstance(x, dict)], key=lambda x: _float(x.get('price')), reverse=True)
    return sorted([x for x in levels if isinstance(x, dict)], key=lambda x: _float(x.get('price')))


def _level_size(level: dict) -> float:
    return _float(level.get('size') or level.get('shares') or level.get('amount'))


def _best_bid_ask(book: dict) -> tuple[float, float, float]:
    bids = _book_levels(book, 'BUY')
    asks = _book_levels(book, 'SELL')
    bid = _float(bids[0].get('price')) if bids else 0.0
    ask = _float(asks[0].get('price')) if asks else 0.0
    spread = ask - bid if bid > 0 and ask > bid else 0.0
    return bid, ask, spread


def _safe_rate(num: float, den: float) -> float:
    return num / den if den else 0.0


class QueuePartialFillAuditor:
    """Paper-only queue and partial-fill realism auditor.

    V8.1 separates quote realism from fill realism. Quote risk can be high for
    deep passive churn levels, but the critical test is whether actual simulated
    fills look plausible after visible queue/depth pressure.
    """

    def __init__(self) -> None:
        self.data_dir = Path(settings.data_dir)
        self.quotes_path = self.data_dir / 'paper_maker_quotes.jsonl'
        self.fills_path = self.data_dir / 'paper_maker_fills.jsonl'
        self.markout_path = self.data_dir / 'paper_maker_markout.json'
        self.execution_path = self.data_dir / 'paper_maker_execution_audit.json'
        self.summary_path = self.data_dir / 'paper_maker_summary.json'
        self.audit_path = self.data_dir / 'paper_maker_queue_audit.json'
        self.runs_path = self.data_dir / 'paper_maker_queue_audit_runs.jsonl'
        self.clob_url = settings.polymarket_clob_url.rstrip('/')
        self.quote_tail = _int_env('PAPER_MAKER_QUEUE_AUDIT_QUOTE_TAIL', 1500)
        self.fill_tail = _int_env('PAPER_MAKER_QUEUE_AUDIT_FILL_TAIL', 1500)
        self.max_tokens = _int_env('PAPER_MAKER_QUEUE_AUDIT_MAX_TOKENS', 100)
        self.timeout = _float_env('PAPER_MAKER_QUEUE_AUDIT_BOOK_TIMEOUT', 2.0)
        self.max_queue_notional_ok = _float_env('PAPER_MAKER_QUEUE_MAX_NOTIONAL_OK', 75.0)
        self.max_queue_multiple_ok = _float_env('PAPER_MAKER_QUEUE_MAX_MULTIPLE_OK', 12.0)
        self.min_partial_fill_rate_ok = _float_env('PAPER_MAKER_MIN_PARTIAL_FILL_RATE_OK', 0.20)
        self.post_base44 = _bool_env('PAPER_MAKER_QUEUE_AUDIT_POST_BASE44', True)

    def _fetch_book(self, token_id: str) -> dict | None:
        try:
            resp = requests.get(f'{self.clob_url}/book', params={'token_id': token_id}, timeout=self.timeout)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _queue_row(self, obj: dict, book: dict | None, kind: str) -> dict:
        token_short = obj.get('token_id_short') or str(obj.get('token_id', ''))[:10]
        side = str(obj.get('side') or '').upper()
        price = _float(obj.get('price') if obj.get('price') is not None else obj.get('fill_price'))
        size_usd = _float(obj.get('size_usd')) or _float(obj.get('notional_usd')) or (_float(obj.get('shares')) * max(price, 0.01))
        ts_key = 'filled_at' if kind == 'fill' else 'created_at'
        age = max(0.0, time.time() - _parse_ts(obj.get(ts_key)))
        row: dict[str, Any] = {
            'kind': kind,
            'quote_id': obj.get('quote_id'),
            'token_id_short': token_short,
            'market_id': obj.get('market_id'),
            'side': side,
            'price': round(price, 6),
            'size_usd': round(size_usd, 6),
            'age_seconds': round(age, 2),
            'book_available': bool(book),
        }
        if not book or side not in {'BUY', 'SELL'} or price <= 0:
            row.update({'queue_status': 'BOOK_UNAVAILABLE', 'queue_risk_score': 90})
            return row
        best_bid, best_ask, spread = _best_bid_ask(book)
        levels = _book_levels(book, side)
        shares = size_usd / max(price, 0.01)
        better_shares = 0.0
        same_level_shares = 0.0
        for lvl in levels:
            p = _float(lvl.get('price'))
            sz = _level_size(lvl)
            if side == 'BUY':
                if p > price:
                    better_shares += sz
                elif abs(p - price) < 1e-9:
                    same_level_shares += sz
            else:
                if p < price:
                    better_shares += sz
                elif abs(p - price) < 1e-9:
                    same_level_shares += sz
        queue_ahead_shares = better_shares + same_level_shares
        queue_ahead_notional = queue_ahead_shares * price
        queue_multiple = queue_ahead_notional / max(size_usd, 0.01)
        visible_partial = min(1.0, shares / max(queue_ahead_shares + shares, 1e-9))
        if side == 'BUY':
            at_top = abs(price - best_bid) < 1e-9 or price > best_bid
            executable_edge = best_bid - price
        else:
            at_top = abs(price - best_ask) < 1e-9 or price < best_ask
            executable_edge = price - best_ask
        risk = 0
        if not at_top:
            risk += 30
        if queue_ahead_notional > self.max_queue_notional_ok:
            risk += 20
        if queue_multiple > self.max_queue_multiple_ok:
            risk += 20
        if visible_partial < self.min_partial_fill_rate_ok:
            risk += 20
        if spread > 0.025:
            risk += 10
        if executable_edge < 0:
            risk += 20
        risk = min(100, risk)
        if risk <= 25:
            status = 'QUEUE_REALISM_OK'
        elif risk <= 55:
            status = 'QUEUE_REALISM_WATCH'
        else:
            status = 'QUEUE_REALISM_RISK'
        row.update({
            'queue_status': status,
            'queue_risk_score': risk,
            'current_best_bid': round(best_bid, 6),
            'current_best_ask': round(best_ask, 6),
            'current_spread': round(spread, 6),
            'at_top_or_better': at_top,
            'executable_edge': round(executable_edge, 6),
            'queue_ahead_notional_usd': round(queue_ahead_notional, 6),
            'queue_multiple_of_order': round(queue_multiple, 6),
            'visible_partial_fill_rate_estimate': round(visible_partial, 6),
        })
        return row

    def _summarize_rows(self, rows: list[dict]) -> dict:
        total = len(rows)
        ok = sum(1 for r in rows if r.get('queue_status') == 'QUEUE_REALISM_OK')
        watch = sum(1 for r in rows if r.get('queue_status') == 'QUEUE_REALISM_WATCH')
        risk = sum(1 for r in rows if r.get('queue_status') == 'QUEUE_REALISM_RISK')
        unavailable = sum(1 for r in rows if r.get('queue_status') == 'BOOK_UNAVAILABLE')
        return {
            'measured': total,
            'ok': ok,
            'watch': watch,
            'risk': risk,
            'unavailable': unavailable,
            'ok_rate': round(_safe_rate(ok, total), 6),
            'watch_rate': round(_safe_rate(watch, total), 6),
            'risk_rate': round(_safe_rate(risk, total), 6),
            'avg_risk_score': round(sum(_float(r.get('queue_risk_score')) for r in rows) / max(1, total), 4),
            'avg_queue_ahead_notional_usd': round(sum(_float(r.get('queue_ahead_notional_usd')) for r in rows) / max(1, total), 6),
            'avg_visible_partial_fill_rate_estimate': round(sum(_float(r.get('visible_partial_fill_rate_estimate')) for r in rows) / max(1, total), 6),
            'at_top_or_better_rate': round(_safe_rate(sum(1 for r in rows if r.get('at_top_or_better')), total), 6),
        }

    def _token_summary(self, rows: list[dict]) -> list[dict]:
        by_token = defaultdict(lambda: {'n': 0, 'risk_sum': 0.0, 'ok': 0, 'watch': 0, 'risk': 0, 'queue': 0.0, 'partial': 0.0})
        for r in rows:
            t = str(r.get('token_id_short') or '')
            by_token[t]['n'] += 1
            by_token[t]['risk_sum'] += _float(r.get('queue_risk_score'))
            by_token[t]['queue'] += _float(r.get('queue_ahead_notional_usd'))
            by_token[t]['partial'] += _float(r.get('visible_partial_fill_rate_estimate'))
            if r.get('queue_status') == 'QUEUE_REALISM_OK':
                by_token[t]['ok'] += 1
            elif r.get('queue_status') == 'QUEUE_REALISM_WATCH':
                by_token[t]['watch'] += 1
            elif r.get('queue_status') == 'QUEUE_REALISM_RISK':
                by_token[t]['risk'] += 1
        out = []
        for token, v in by_token.items():
            n = max(1, int(v['n']))
            out.append({
                'token': token,
                'rows': int(v['n']),
                'avg_queue_risk_score': round(v['risk_sum'] / n, 4),
                'ok_rate': round(v['ok'] / n, 6),
                'watch_rate': round(v['watch'] / n, 6),
                'risk_rate': round(v['risk'] / n, 6),
                'avg_queue_ahead_notional_usd': round(v['queue'] / n, 6),
                'avg_partial_fill_rate_estimate': round(v['partial'] / n, 6),
            })
        out.sort(key=lambda x: (x['avg_queue_risk_score'], -x['rows']), reverse=True)
        return out[:20]

    def run(self) -> dict:
        quotes = _read_jsonl_tail(self.quotes_path, self.quote_tail)
        fills = _read_jsonl_tail(self.fills_path, self.fill_tail)
        markout: dict[str, Any] = {}
        execution: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        for path, target in [(self.markout_path, markout), (self.execution_path, execution), (self.summary_path, summary)]:
            if path.exists():
                try:
                    target.update(json.loads(path.read_text()))
                except Exception:
                    pass
        token_ids: list[str] = []
        for source in (reversed(fills), reversed(quotes)):
            for row in source:
                token = str(row.get('token_id') or '')
                if token and token not in token_ids:
                    token_ids.append(token)
                if len(token_ids) >= self.max_tokens:
                    break
            if len(token_ids) >= self.max_tokens:
                break
        books = {t: self._fetch_book(t) for t in token_ids}
        quote_rows = [self._queue_row(q, books.get(str(q.get('token_id') or '')), 'quote') for q in quotes[-500:]]
        fill_rows = [self._queue_row(f, books.get(str(f.get('token_id') or '')), 'fill') for f in fills[-500:]]
        quote_stats = self._summarize_rows(quote_rows)
        fill_stats = self._summarize_rows(fill_rows)

        fill_adjusted_exec_pnl = _float(markout.get('executable_markout_usd')) * max(0.0, fill_stats['ok_rate'] + 0.5 * fill_stats['watch_rate'])
        verdict = 'QUEUE_PARTIAL_NEEDS_MORE_DATA'
        if fill_stats['measured'] >= 100 and fill_stats['risk_rate'] <= 0.30 and fill_stats['avg_risk_score'] <= 55:
            verdict = 'FILL_QUEUE_PLAUSIBLE'
        if fill_stats['measured'] >= 100 and (fill_stats['risk_rate'] > 0.60 or fill_stats['avg_visible_partial_fill_rate_estimate'] < 0.05):
            verdict = 'FILL_QUEUE_RISK'
        if fill_stats['measured'] < 100 and quote_stats['risk_rate'] > 0.75:
            verdict = 'QUOTE_QUEUE_RISK_NEEDS_FILL_SAMPLE'

        audit = {
            'generated_at': _now_iso(),
            'mode': 'PAPER_QUEUE_PARTIAL_AUDIT_V8_1',
            'paper_only': True,
            'live_orders_enabled': False,
            'verdict': verdict,
            'quotes_measured': quote_stats['measured'],
            'fills_measured_for_queue': fill_stats['measured'],
            'fills_seen_tail': len(fills),
            'tokens_checked': len(token_ids),
            'books_available': sum(1 for b in books.values() if b),
            'quote_queue_ok_rate': quote_stats['ok_rate'],
            'quote_queue_watch_rate': quote_stats['watch_rate'],
            'quote_queue_risk_rate': quote_stats['risk_rate'],
            'quote_avg_queue_risk_score': quote_stats['avg_risk_score'],
            'quote_avg_visible_partial_fill_rate_estimate': quote_stats['avg_visible_partial_fill_rate_estimate'],
            'quote_at_top_or_better_rate': quote_stats['at_top_or_better_rate'],
            'fill_queue_ok_rate': fill_stats['ok_rate'],
            'fill_queue_watch_rate': fill_stats['watch_rate'],
            'fill_queue_risk_rate': fill_stats['risk_rate'],
            'fill_avg_queue_risk_score': fill_stats['avg_risk_score'],
            'fill_avg_queue_ahead_notional_usd': fill_stats['avg_queue_ahead_notional_usd'],
            'fill_avg_visible_partial_fill_rate_estimate': fill_stats['avg_visible_partial_fill_rate_estimate'],
            'fill_at_top_or_better_rate': fill_stats['at_top_or_better_rate'],
            'strict_markout_verdict': markout.get('verdict'),
            'strict_measured_fills': markout.get('measured_fills'),
            'strict_executable_markout_usd': markout.get('executable_markout_usd'),
            'strict_positive_executable_markout_rate': markout.get('positive_executable_markout_rate'),
            'queue_adjusted_executable_markout_usd': round(fill_adjusted_exec_pnl, 6),
            'execution_dry_run_verdict': execution.get('verdict'),
            'execution_ok_rate': execution.get('execution_ok_rate'),
            'avg_execution_risk_score': execution.get('avg_execution_risk_score'),
            'orders_simulated_today': summary.get('orders_simulated_today'),
            'fills_simulated_today': summary.get('fills_simulated_today'),
            'inventory_exposure_usd': summary.get('inventory_exposure_usd'),
            'top_quote_queue_risk_tokens': self._token_summary(quote_rows),
            'top_fill_queue_risk_tokens': self._token_summary(fill_rows),
            'recent_quote_queue_rows_sample': sorted(quote_rows, key=lambda x: _float(x.get('queue_risk_score')), reverse=True)[:25],
            'recent_fill_queue_rows_sample': sorted(fill_rows, key=lambda x: _float(x.get('queue_risk_score')), reverse=True)[:25],
            'paper_only_note': 'V8.1 queue/partial-fill audit. It separates quote queue risk from actual paper fill queue plausibility and never submits orders.',
        }
        self.audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        with self.runs_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(audit, ensure_ascii=False, default=str) + '\n')
        if self.post_base44 and settings.base44_write_enabled:
            base44.post_record('MakerQueueAudit', audit)
        log.info('paper_maker_queue_audit_v8_1 verdict=%s quotes=%s quote_risk=%.4f fills=%s fill_risk=%.4f fill_avg_risk=%.2f adjusted_exec=%s', verdict, quote_stats['measured'], quote_stats['risk_rate'], fill_stats['measured'], fill_stats['risk_rate'], fill_stats['avg_risk_score'], audit['queue_adjusted_executable_markout_usd'])
        return audit


def run_queue_audit() -> dict:
    return QueuePartialFillAuditor().run()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_queue_audit(), ensure_ascii=False, indent=2, default=str))
