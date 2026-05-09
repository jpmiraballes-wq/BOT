from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from base44_client import base44
from config import settings


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _iter_jsonl(path: Path, limit: int = 20000) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(errors='ignore').splitlines()[-limit:]:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                pass
    except Exception:
        return []
    return rows


def _best_levels(book: dict) -> tuple[float, float, float, float]:
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
    return best_bid, best_ask, midpoint, spread


def _fetch_book(token_id: str, timeout: float = 2.0) -> dict | None:
    try:
        url = settings.polymarket_clob_url.rstrip('/') + '/book'
        r = requests.get(url, params={'token_id': token_id}, timeout=timeout)
        if r.status_code >= 400:
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _score(value: float, good: float, bad: float, higher_is_better: bool = True) -> float:
    if higher_is_better:
        if value >= good:
            return 100.0
        if value <= bad:
            return 0.0
        return 100.0 * (value - bad) / max(1e-9, good - bad)
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / max(1e-9, bad - good)


def run_markout_audit(min_age_seconds: int = 60, max_fills: int = 1500) -> dict:
    """Measure adverse selection using real current Polymarket books.

    For each paper fill older than min_age_seconds, fetch current top-of-book and
    compare current midpoint vs the simulated fill price. BUY fills are good when
    current mid > fill price; SELL fills are good when fill price > current mid.
    This is intentionally conservative: it does not prove actual execution, but
    it tells us whether simulated fills would have been picked off or not.
    """
    data_dir = Path(settings.data_dir)
    fills_path = data_dir / 'paper_maker_fills.jsonl'
    out_path = data_dir / 'paper_maker_markout.json'

    now = datetime.now(timezone.utc)
    fills = _iter_jsonl(fills_path, limit=max_fills * 3)
    eligible = []
    for f in fills:
        dt = _parse_dt(f.get('filled_at'))
        if not dt:
            continue
        age = (now - dt).total_seconds()
        if age >= min_age_seconds and f.get('token_id'):
            f['_age_seconds'] = age
            eligible.append(f)
    eligible = eligible[-max_fills:]

    books_by_token: dict[str, dict] = {}
    levels_by_token: dict[str, tuple[float, float, float, float]] = {}
    for token in sorted({str(f.get('token_id')) for f in eligible if f.get('token_id')}):
        book = _fetch_book(token)
        if not book:
            continue
        levels = _best_levels(book)
        if levels[2] > 0:
            books_by_token[token] = book
            levels_by_token[token] = levels

    rows = []
    by_token = defaultdict(lambda: {'fills': 0, 'notional': 0.0, 'markout_usd': 0.0, 'positive': 0})
    by_market = defaultdict(lambda: {'fills': 0, 'notional': 0.0, 'markout_usd': 0.0, 'positive': 0})

    total_notional = 0.0
    total_markout_usd = 0.0
    positive_count = 0
    measured_count = 0

    for f in eligible:
        token = str(f.get('token_id'))
        levels = levels_by_token.get(token)
        if not levels:
            continue
        bid, ask, mid, spread = levels
        side = str(f.get('side') or '').upper()
        price = _float(f.get('price'))
        shares = _float(f.get('shares'))
        notional = _float(f.get('notional_usd'), shares * price)
        if price <= 0 or shares <= 0 or side not in {'BUY', 'SELL'}:
            continue
        if side == 'BUY':
            markout_per_share = mid - price
        else:
            markout_per_share = price - mid
        markout_usd = markout_per_share * shares
        measured_count += 1
        total_notional += notional
        total_markout_usd += markout_usd
        if markout_usd > 0:
            positive_count += 1
        token_short = str(f.get('token_id_short') or token[:10])
        market_id = str(f.get('market_id') or '')
        by_token[token_short]['fills'] += 1
        by_token[token_short]['notional'] += notional
        by_token[token_short]['markout_usd'] += markout_usd
        by_token[token_short]['positive'] += 1 if markout_usd > 0 else 0
        by_market[market_id]['fills'] += 1
        by_market[market_id]['notional'] += notional
        by_market[market_id]['markout_usd'] += markout_usd
        by_market[market_id]['positive'] += 1 if markout_usd > 0 else 0
        rows.append({
            'filled_at': f.get('filled_at'),
            'age_seconds': round(_float(f.get('_age_seconds')), 1),
            'market_id': market_id,
            'token_id_short': token_short,
            'side': side,
            'fill_price': price,
            'current_bid': bid,
            'current_ask': ask,
            'current_midpoint': mid,
            'current_spread': spread,
            'shares': round(shares, 6),
            'notional_usd': round(notional, 6),
            'markout_per_share': round(markout_per_share, 6),
            'markout_usd': round(markout_usd, 6),
            'adverse': markout_usd < 0,
        })

    positive_rate = positive_count / max(1, measured_count)
    markout_bps = 10000.0 * total_markout_usd / max(1.0, total_notional)
    avg_markout_per_fill = total_markout_usd / max(1, measured_count)

    markout_score = round(
        0.45 * _score(markout_bps, good=15.0, bad=-25.0, higher_is_better=True)
        + 0.35 * _score(positive_rate, good=0.58, bad=0.42, higher_is_better=True)
        + 0.20 * _score(measured_count, good=250, bad=25, higher_is_better=True),
        2,
    )

    if measured_count < 50:
        verdict = 'NEEDS_MORE_FILLS'
    elif markout_score >= 70 and total_markout_usd > 0:
        verdict = 'MARKOUT_GOOD'
    elif markout_score >= 50:
        verdict = 'MARKOUT_MIXED'
    else:
        verdict = 'ADVERSE_SELECTION_RISK'

    audit = {
        'generated_at': now.isoformat(),
        'verdict': verdict,
        'markout_score': markout_score,
        'min_age_seconds': min_age_seconds,
        'eligible_fills': len(eligible),
        'measured_fills': measured_count,
        'tokens_measured': len(levels_by_token),
        'total_notional_usd': round(total_notional, 6),
        'total_markout_usd': round(total_markout_usd, 6),
        'avg_markout_usd_per_fill': round(avg_markout_per_fill, 6),
        'markout_bps_on_notional': round(markout_bps, 4),
        'positive_markout_rate': round(positive_rate, 6),
        'top_positive_tokens': sorted(
            [
                {
                    'token': k,
                    'fills': int(v['fills']),
                    'notional_usd': round(v['notional'], 6),
                    'markout_usd': round(v['markout_usd'], 6),
                    'positive_rate': round(v['positive'] / max(1, v['fills']), 6),
                }
                for k, v in by_token.items()
            ],
            key=lambda x: x['markout_usd'],
            reverse=True,
        )[:10],
        'top_adverse_tokens': sorted(
            [
                {
                    'token': k,
                    'fills': int(v['fills']),
                    'notional_usd': round(v['notional'], 6),
                    'markout_usd': round(v['markout_usd'], 6),
                    'positive_rate': round(v['positive'] / max(1, v['fills']), 6),
                }
                for k, v in by_token.items()
            ],
            key=lambda x: x['markout_usd'],
        )[:10],
        'recent_rows_sample': sorted(rows, key=lambda x: abs(x['markout_usd']), reverse=True)[:50],
        'paper_only_note': 'Markout compares paper fill prices against current real Polymarket top-of-book. It is a realism/adverse-selection audit, not live execution proof.',
    }
    out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    if settings.base44_write_enabled:
        base44.post_record('MakerMarkoutAudit', audit)
    return audit


if __name__ == '__main__':
    print(json.dumps(run_markout_audit(), ensure_ascii=False, indent=2, default=str))
