from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from base44_client import base44
from config import settings
from paper_maker_engine import _float, _now_iso

log = logging.getLogger('paper_maker_execution_audit')


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


def _best_levels(book: dict) -> tuple[float, float, float, float, float, float]:
    bids = book.get('bids') or []
    asks = book.get('asks') or []
    best_bid = max([_float(x.get('price')) for x in bids if isinstance(x, dict)] or [0.0])
    best_ask = min([_float(x.get('price'), 1.0) for x in asks if isinstance(x, dict)] or [0.0])
    bid_size = sum(_float(x.get('size') or x.get('shares')) for x in bids if isinstance(x, dict) and abs(_float(x.get('price')) - best_bid) < 1e-9)
    ask_size = sum(_float(x.get('size') or x.get('shares')) for x in asks if isinstance(x, dict) and abs(_float(x.get('price')) - best_ask) < 1e-9)
    midpoint = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > best_bid else 0.0
    spread = best_ask - best_bid if best_bid > 0 and best_ask > best_bid else 0.0
    return best_bid, best_ask, midpoint, spread, bid_size, ask_size


class ExecutionRealityAuditor:
    """Paper-only execution realism audit.

    Never signs or submits orders. It only reads paper quotes/fills and public CLOB
    books to estimate live-like friction: queue proxy, stale quote risk, spread
    crossing risk, and book availability.
    """

    def __init__(self) -> None:
        self.data_dir = Path(settings.data_dir)
        self.quotes_path = self.data_dir / 'paper_maker_quotes.jsonl'
        self.fills_path = self.data_dir / 'paper_maker_fills.jsonl'
        self.summary_path = self.data_dir / 'paper_maker_summary.json'
        self.markout_path = self.data_dir / 'paper_maker_markout.json'
        self.audit_path = self.data_dir / 'paper_maker_execution_audit.json'
        self.runs_path = self.data_dir / 'paper_maker_execution_audit_runs.jsonl'
        self.clob_url = settings.polymarket_clob_url.rstrip('/')
        self.quote_tail = _int_env('PAPER_MAKER_EXEC_AUDIT_QUOTE_TAIL', 1500)
        self.fill_tail = _int_env('PAPER_MAKER_EXEC_AUDIT_FILL_TAIL', 1500)
        self.max_tokens = _int_env('PAPER_MAKER_EXEC_AUDIT_MAX_TOKENS', 80)
        self.book_timeout = _float_env('PAPER_MAKER_EXEC_AUDIT_BOOK_TIMEOUT', 2.0)
        self.assumed_latency_ms = _float_env('PAPER_MAKER_EXEC_AUDIT_LATENCY_MS', 750.0)
        self.assumed_cancel_ms = _float_env('PAPER_MAKER_EXEC_AUDIT_CANCEL_MS', 500.0)
        self.max_good_spread = _float_env('PAPER_MAKER_EXEC_AUDIT_GOOD_SPREAD', 0.025)
        self.post_base44 = _bool_env('PAPER_MAKER_EXEC_AUDIT_POST_BASE44', True)

    def _fetch_book(self, token_id: str) -> dict | None:
        try:
            resp = requests.get(f'{self.clob_url}/book', params={'token_id': token_id}, timeout=self.book_timeout)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _execution_status(self, q: dict, book: dict | None) -> dict:
        side = str(q.get('side') or '').upper()
        price = _float(q.get('price'))
        size_usd = _float(q.get('size_usd')) or (_float(q.get('shares')) * price)
        out: dict[str, Any] = {
            'quote_id': q.get('quote_id'),
            'token_id_short': q.get('token_id_short') or str(q.get('token_id', ''))[:10],
            'market_id': q.get('market_id'),
            'side': side,
            'quote_price': round(price, 6),
            'quote_size_usd': round(size_usd, 6),
            'book_available': bool(book),
        }
        if not book:
            out.update({'status': 'BOOK_UNAVAILABLE', 'risk_score': 80})
            return out
        bid, ask, mid, spread, bid_size, ask_size = _best_levels(book)
        out.update({
            'current_best_bid': round(bid, 6),
            'current_best_ask': round(ask, 6),
            'current_midpoint': round(mid, 6),
            'current_spread': round(spread, 6),
            'best_bid_size': round(bid_size, 6),
            'best_ask_size': round(ask_size, 6),
        })
        if bid <= 0 or ask <= 0 or ask <= bid:
            out.update({'status': 'LOCKED_OR_INVALID_BOOK', 'risk_score': 85})
            return out
        if side == 'BUY':
            executable_edge = bid - price
            at_or_inside = price <= bid
            queue_proxy_shares = bid_size if abs(price - bid) < 1e-9 else 0.0
            crosses = price >= ask
        else:
            executable_edge = price - ask
            at_or_inside = price >= ask
            queue_proxy_shares = ask_size if abs(price - ask) < 1e-9 else 0.0
            crosses = price <= bid
        queue_notional_proxy = queue_proxy_shares * max(price, 0.01)
        quote_age_seconds = max(0.0, time.time() - _parse_ts(q.get('created_at')))
        stale = quote_age_seconds > 180
        wide = spread > self.max_good_spread
        risk = 0
        if not at_or_inside:
            risk += 35
        if executable_edge < 0:
            risk += 30
        if wide:
            risk += 15
        if stale:
            risk += 10
        if queue_notional_proxy > max(10.0, size_usd * 5.0):
            risk += 10
        if crosses:
            risk += 25
        risk = min(100, risk)
        status = 'EXECUTION_REALISM_OK' if risk <= 20 else ('EXECUTION_REALISM_WATCH' if risk <= 45 else 'EXECUTION_REALISM_RISK')
        out.update({
            'status': status,
            'risk_score': risk,
            'executable_edge': round(executable_edge, 6),
            'quote_age_seconds': round(quote_age_seconds, 2),
            'stale_quote': stale,
            'wide_spread': wide,
            'would_cross_spread': crosses,
            'at_or_inside_executable_side': at_or_inside,
            'queue_notional_proxy_usd': round(queue_notional_proxy, 6),
            'assumed_latency_ms': self.assumed_latency_ms,
            'assumed_cancel_ms': self.assumed_cancel_ms,
        })
        return out

    def run(self) -> dict:
        quotes = _read_jsonl_tail(self.quotes_path, self.quote_tail)
        fills = _read_jsonl_tail(self.fills_path, self.fill_tail)
        markout: dict[str, Any] = {}
        summary: dict[str, Any] = {}
        if self.markout_path.exists():
            try:
                markout = json.loads(self.markout_path.read_text())
            except Exception:
                pass
        if self.summary_path.exists():
            try:
                summary = json.loads(self.summary_path.read_text())
            except Exception:
                pass
        token_ids: list[str] = []
        for q in reversed(quotes):
            token = str(q.get('token_id') or '')
            if token and token not in token_ids:
                token_ids.append(token)
            if len(token_ids) >= self.max_tokens:
                break
        books = {t: self._fetch_book(t) for t in token_ids}
        rows = [self._execution_status(q, books.get(str(q.get('token_id') or ''))) for q in quotes[-500:]]
        if rows:
            ok = sum(1 for r in rows if r.get('status') == 'EXECUTION_REALISM_OK')
            watch = sum(1 for r in rows if r.get('status') == 'EXECUTION_REALISM_WATCH')
            risk = sum(1 for r in rows if r.get('status') == 'EXECUTION_REALISM_RISK')
            unavailable = sum(1 for r in rows if r.get('status') in {'BOOK_UNAVAILABLE', 'LOCKED_OR_INVALID_BOOK'})
            avg_risk = sum(_float(r.get('risk_score')) for r in rows) / len(rows)
            wide_rate = sum(1 for r in rows if r.get('wide_spread')) / len(rows)
            stale_rate = sum(1 for r in rows if r.get('stale_quote')) / len(rows)
            cross_rate = sum(1 for r in rows if r.get('would_cross_spread')) / len(rows)
            ok_rate = ok / len(rows)
        else:
            ok = watch = risk = unavailable = 0
            avg_risk = wide_rate = stale_rate = cross_rate = ok_rate = 0.0
        by_token = defaultdict(lambda: {'quotes': 0, 'risk_sum': 0.0, 'ok': 0, 'risk': 0})
        for r in rows:
            t = str(r.get('token_id_short') or '')
            by_token[t]['quotes'] += 1
            by_token[t]['risk_sum'] += _float(r.get('risk_score'))
            if r.get('status') == 'EXECUTION_REALISM_OK':
                by_token[t]['ok'] += 1
            if r.get('status') == 'EXECUTION_REALISM_RISK':
                by_token[t]['risk'] += 1
        token_rows = []
        for token, v in by_token.items():
            qn = max(1, int(v['quotes']))
            token_rows.append({
                'token': token,
                'quotes': int(v['quotes']),
                'avg_risk_score': round(v['risk_sum'] / qn, 4),
                'ok_rate': round(v['ok'] / qn, 6),
                'risk_rate': round(v['risk'] / qn, 6),
            })
        token_rows.sort(key=lambda x: (x['avg_risk_score'], -x['quotes']), reverse=True)
        measured_fills = int(markout.get('measured_fills') or 0)
        exec_markout = _float(markout.get('executable_markout_usd'))
        positive_rate = _float(markout.get('positive_executable_markout_rate'))
        verdict = 'EXECUTION_DRY_RUN_NEEDS_MORE_DATA'
        if len(rows) >= 100 and ok_rate >= 0.60 and avg_risk <= 35 and exec_markout > 0 and measured_fills >= 100:
            verdict = 'EXECUTION_DRY_RUN_OK'
        if avg_risk > 55 or cross_rate > 0.10 or wide_rate > 0.10:
            verdict = 'EXECUTION_DRY_RUN_RISK'
        audit = {
            'generated_at': _now_iso(),
            'mode': 'PAPER_EXECUTION_DRY_RUN',
            'paper_only': True,
            'live_orders_enabled': False,
            'verdict': verdict,
            'quotes_measured': len(rows),
            'fills_seen_tail': len(fills),
            'tokens_checked': len(token_ids),
            'books_available': sum(1 for b in books.values() if b),
            'execution_ok_quotes': ok,
            'execution_watch_quotes': watch,
            'execution_risk_quotes': risk,
            'book_unavailable_quotes': unavailable,
            'execution_ok_rate': round(ok_rate, 6),
            'avg_execution_risk_score': round(avg_risk, 4),
            'wide_spread_quote_rate': round(wide_rate, 6),
            'stale_quote_rate': round(stale_rate, 6),
            'would_cross_spread_rate': round(cross_rate, 6),
            'assumed_latency_ms': self.assumed_latency_ms,
            'assumed_cancel_ms': self.assumed_cancel_ms,
            'strict_markout_verdict': markout.get('verdict'),
            'strict_measured_fills': measured_fills,
            'strict_executable_markout_usd': round(exec_markout, 6),
            'strict_positive_executable_markout_rate': round(positive_rate, 6),
            'orders_simulated_today': summary.get('orders_simulated_today'),
            'fills_simulated_today': summary.get('fills_simulated_today'),
            'inventory_exposure_usd': summary.get('inventory_exposure_usd'),
            'top_execution_risk_tokens': token_rows[:20],
            'recent_execution_rows_sample': sorted(rows, key=lambda x: _float(x.get('risk_score')), reverse=True)[:50],
            'paper_only_note': 'Execution dry-run only. It reads CLOB books and paper quotes/fills; it never signs or submits live orders.',
        }
        self.audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        with self.runs_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(audit, ensure_ascii=False, default=str) + '\n')
        if self.post_base44 and settings.base44_write_enabled:
            base44.post_record('MakerExecutionAudit', audit)
        log.info('paper_maker_execution_audit verdict=%s quotes=%s ok_rate=%.4f avg_risk=%.2f strict_fills=%s strict_exec=%.4f', verdict, len(rows), ok_rate, avg_risk, measured_fills, exec_markout)
        return audit


def run_execution_audit() -> dict:
    return ExecutionRealityAuditor().run()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_execution_audit(), ensure_ascii=False, indent=2, default=str))
