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


def _safe_base44_post(entity: str, payload: dict) -> None:
    try:
        base44.post_record(entity, payload)
    except Exception:
        # The audit file on disk is the source of truth. Missing dashboard schema
        # must never break terminal audits or the running maker.
        pass


def run_markout_audit(min_age_seconds: int = 60, max_fills: int = 1500) -> dict:
    """Measure adverse selection using real current Polymarket books.

    Strict mode uses executable-side markout, not midpoint-only markout:
    - BUY fill is marked against current best_bid because that is the immediate
      exit price if we had to sell now.
    - SELL fill is marked against current best_ask because that is the immediate
      buyback price if we had to cover now.

    This is harsher than midpoint markout and avoids self-deception on wide
    markets where midpoint can look profitable but executable exit is not.
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

    levels_by_token: dict[str, tuple[float, float, float, float]] = {}
    for token in sorted({str(f.get('token_id')) for f in eligible if f.get('token_id')}):
        book = _fetch_book(token)
        if not book:
            continue
        levels = _best_levels(book)
        bid, ask, mid, spread = levels
        if mid > 0 and bid > 0 and ask > 0:
            levels_by_token[token] = levels

    rows = []
    by_token = defaultdict(lambda: {'fills': 0, 'notional': 0.0, 'mid_markout_usd': 0.0, 'exec_markout_usd': 0.0, 'positive': 0})
    by_market = defaultdict(lambda: {'fills': 0, 'notional': 0.0, 'exec_markout_usd': 0.0, 'positive': 0})

    total_notional = 0.0
    total_mid_markout_usd = 0.0
    total_exec_markout_usd = 0.0
    positive_count = 0
    measured_count = 0
    wide_spread_count = 0
    stale_or_locked_count = 0

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
        if ask <= bid or spread <= 0:
            stale_or_locked_count += 1
            continue
        if spread > 0.08:
            wide_spread_count += 1

        if side == 'BUY':
            mid_markout_per_share = mid - price
            exec_markout_per_share = bid - price
        else:
            mid_markout_per_share = price - mid
            exec_markout_per_share = price - ask

        mid_markout_usd = mid_markout_per_share * shares
        exec_markout_usd = exec_markout_per_share * shares
        measured_count += 1
        total_notional += notional
        total_mid_markout_usd += mid_markout_usd
        total_exec_markout_usd += exec_markout_usd
        if exec_markout_usd > 0:
            positive_count += 1
        token_short = str(f.get('token_id_short') or token[:10])
        market_id = str(f.get('market_id') or '')
        by_token[token_short]['fills'] += 1
        by_token[token_short]['notional'] += notional
        by_token[token_short]['mid_markout_usd'] += mid_markout_usd
        by_token[token_short]['exec_markout_usd'] += exec_markout_usd
        by_token[token_short]['positive'] += 1 if exec_markout_usd > 0 else 0
        by_market[market_id]['fills'] += 1
        by_market[market_id]['notional'] += notional
        by_market[market_id]['exec_markout_usd'] += exec_markout_usd
        by_market[market_id]['positive'] += 1 if exec_markout_usd > 0 else 0
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
            'mid_markout_per_share': round(mid_markout_per_share, 6),
            'mid_markout_usd': round(mid_markout_usd, 6),
            'exec_markout_per_share': round(exec_markout_per_share, 6),
            'exec_markout_usd': round(exec_markout_usd, 6),
            'adverse': exec_markout_usd < 0,
            'wide_spread': spread > 0.08,
        })

    positive_rate = positive_count / max(1, measured_count)
    mid_markout_bps = 10000.0 * total_mid_markout_usd / max(1.0, total_notional)
    exec_markout_bps = 10000.0 * total_exec_markout_usd / max(1.0, total_notional)
    avg_exec_markout_per_fill = total_exec_markout_usd / max(1, measured_count)
    wide_spread_rate = wide_spread_count / max(1, measured_count)

    markout_score = round(
        0.45 * _score(exec_markout_bps, good=5.0, bad=-35.0, higher_is_better=True)
        + 0.25 * _score(positive_rate, good=0.54, bad=0.40, higher_is_better=True)
        + 0.15 * _score(measured_count, good=250, bad=25, higher_is_better=True)
        + 0.15 * _score(wide_spread_rate, good=0.10, bad=0.45, higher_is_better=False),
        2,
    )

    warnings = []
    if wide_spread_rate > 0.25:
        warnings.append('many_fills_marked_on_wide_spread_books')
    if total_mid_markout_usd > 0 and total_exec_markout_usd < 0:
        warnings.append('midpoint_profitable_but_executable_exit_negative')
    if positive_rate < 0.45:
        warnings.append('low_positive_executable_markout_rate')

    if measured_count < 50:
        verdict = 'NEEDS_MORE_FILLS'
    elif markout_score >= 70 and total_exec_markout_usd > 0 and not warnings:
        verdict = 'MARKOUT_GOOD_STRICT'
    elif markout_score >= 50 and total_exec_markout_usd > 0:
        verdict = 'MARKOUT_MIXED_STRICT'
    else:
        verdict = 'ADVERSE_SELECTION_RISK_STRICT'

    audit = {
        'generated_at': now.isoformat(),
        'verdict': verdict,
        'markout_score': markout_score,
        'min_age_seconds': min_age_seconds,
        'eligible_fills': len(eligible),
        'measured_fills': measured_count,
        'tokens_measured': len(levels_by_token),
        'total_notional_usd': round(total_notional, 6),
        'midpoint_markout_usd': round(total_mid_markout_usd, 6),
        'executable_markout_usd': round(total_exec_markout_usd, 6),
        'avg_executable_markout_usd_per_fill': round(avg_exec_markout_per_fill, 6),
        'midpoint_markout_bps_on_notional': round(mid_markout_bps, 4),
        'executable_markout_bps_on_notional': round(exec_markout_bps, 4),
        'positive_executable_markout_rate': round(positive_rate, 6),
        'wide_spread_fill_rate': round(wide_spread_rate, 6),
        'wide_spread_fills': int(wide_spread_count),
        'stale_or_locked_books_skipped': int(stale_or_locked_count),
        'warnings': warnings,
        'top_positive_tokens': sorted(
            [
                {
                    'token': k,
                    'fills': int(v['fills']),
                    'notional_usd': round(v['notional'], 6),
                    'midpoint_markout_usd': round(v['mid_markout_usd'], 6),
                    'executable_markout_usd': round(v['exec_markout_usd'], 6),
                    'positive_rate': round(v['positive'] / max(1, v['fills']), 6),
                }
                for k, v in by_token.items()
            ],
            key=lambda x: x['executable_markout_usd'],
            reverse=True,
        )[:10],
        'top_adverse_tokens': sorted(
            [
                {
                    'token': k,
                    'fills': int(v['fills']),
                    'notional_usd': round(v['notional'], 6),
                    'midpoint_markout_usd': round(v['mid_markout_usd'], 6),
                    'executable_markout_usd': round(v['exec_markout_usd'], 6),
                    'positive_rate': round(v['positive'] / max(1, v['fills']), 6),
                }
                for k, v in by_token.items()
            ],
            key=lambda x: x['executable_markout_usd'],
        )[:10],
        'recent_rows_sample': sorted(rows, key=lambda x: abs(x['exec_markout_usd']), reverse=True)[:50],
        'paper_only_note': 'Strict markout compares paper fill prices against current executable side of real Polymarket top-of-book. BUY exits at bid; SELL covers at ask.',
    }
    out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    if settings.base44_write_enabled:
        _safe_base44_post('MakerMarkoutAudit', audit)
    return audit


if __name__ == '__main__':
    print(json.dumps(run_markout_audit(), ensure_ascii=False, indent=2, default=str))
