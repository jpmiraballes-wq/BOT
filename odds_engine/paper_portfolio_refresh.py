from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from config import Settings, settings as default_settings

log = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def _open_unique_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_id = str(row.get('id') or '').strip()
        if not trade_id:
            continue
        # Latest row wins if the same trade is later closed/updated.
        by_id[trade_id] = row
    return [row for row in by_id.values() if str(row.get('status') or '').lower() == 'open']


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def _fetch_mark_price(token_id: str, cfg: Settings) -> tuple[float | None, str]:
    token_id = str(token_id or '').strip()
    if not token_id:
        return None, 'missing_token'
    url = f"{cfg.polymarket_clob_url.rstrip('/')}/price"
    # For an open YES long, clob SELL is the conservative mark-to-exit price.
    try:
        resp = requests.get(url, params={'token_id': token_id, 'side': 'sell'}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = data.get('price') if isinstance(data, dict) else None
        p = _safe_float(price, -1.0)
        if 0.0 < p < 1.0:
            return p, 'clob_sell'
    except Exception as exc:
        log.info('paper_mark_price_failed token=%s err=%s', token_id[:10], exc)
    return None, 'mark_unavailable'


def _duplicate_count(rows: list[dict[str, Any]], trade_id: str) -> int:
    return sum(1 for row in rows if str(row.get('id') or '') == trade_id)


def refresh_paper_portfolio(cfg: Settings | None = None) -> dict[str, Any]:
    """Mark all open paper trades and regenerate paper_portfolio_summary.json.

    This is intentionally local-file only. It never creates trades, never changes
    risk, and never touches Base44. It just keeps the paper portfolio current
    immediately after a papertrade.jsonl write.
    """
    cfg = cfg or default_settings
    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    trades_path = data_dir / 'papertrade.jsonl'
    marks_path = data_dir / 'papertrade_marks.jsonl'
    summary_path = data_dir / 'paper_portfolio_summary.json'

    trade_rows = _load_jsonl(trades_path)
    open_trades = _open_unique_trades(trade_rows)
    marked_at = _utc_now()
    marks: list[dict[str, Any]] = []
    ok = 0
    failed = 0

    for trade in open_trades:
        trade_id = str(trade.get('id') or '')
        token_id = str(trade.get('token_id') or '')
        entry = _safe_float(trade.get('entry_price'))
        size = _safe_float(trade.get('size_usd'))
        qty = _safe_float(trade.get('quantity'))
        mark_price, source = _fetch_mark_price(token_id, cfg)
        if mark_price is None:
            mark_price = entry
            failed += 1
        else:
            ok += 1
        pnl_usd = round((mark_price - entry) * qty, 6)
        pnl_pct = round(((mark_price / entry) - 1.0) * 100.0, 4) if entry > 0 else 0.0
        marks.append({
            'trade_id': trade_id,
            'signal_id': trade.get('signal_id'),
            'external_event_id': trade.get('external_event_id'),
            'polymarket_market_id': trade.get('polymarket_market_id'),
            'token_id': token_id,
            'side': trade.get('side') or 'YES',
            'status': 'open_marked',
            'entry_price': entry,
            'mark_price': round(mark_price, 6),
            'price_source': source,
            'size_usd': size,
            'quantity': qty,
            'unrealized_pnl_usd': pnl_usd,
            'unrealized_pnl_pct': pnl_pct,
            'duplicate_open_rows': _duplicate_count(trade_rows, trade_id),
            'opened_at': trade.get('opened_at'),
            'reason_open': trade.get('reason_open'),
            'marked_at': marked_at,
        })

    if marks:
        with marks_path.open('a', encoding='utf-8') as f:
            for mark in marks:
                f.write(json.dumps(mark, ensure_ascii=False, default=str) + '\n')

    total_exposure = round(sum(_safe_float(m.get('size_usd')) for m in marks), 6)
    total_pnl = round(sum(_safe_float(m.get('unrealized_pnl_usd')) for m in marks), 6)
    total_pct = round((total_pnl / total_exposure) * 100.0, 4) if total_exposure > 0 else 0.0
    positions = sorted(marks, key=lambda x: str(x.get('opened_at') or ''))
    best = max(positions, key=lambda x: _safe_float(x.get('unrealized_pnl_usd')), default=None)
    worst = min(positions, key=lambda x: _safe_float(x.get('unrealized_pnl_usd')), default=None)

    summary = {
        'generated_at': marked_at,
        'source_files': {
            'trades': str(trades_path),
            'marks': str(marks_path),
        },
        'counts': {
            'raw_trade_rows': len(trade_rows),
            'raw_mark_rows': len(_load_jsonl(marks_path)),
            'open_unique_positions': len(open_trades),
            'duplicate_open_rows_extra': max(0, len([r for r in trade_rows if str(r.get('status') or '').lower() == 'open']) - len(open_trades)),
        },
        'portfolio': {
            'total_exposure_usd': total_exposure,
            'total_unrealized_pnl_usd': total_pnl,
            'total_unrealized_pnl_pct_on_exposure': total_pct,
        },
        'best_position': best,
        'worst_position': worst,
        'positions': positions,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    log.info(
        'paper_portfolio_refresh_complete open_unique=%s marks=%s ok=%s failed=%s summary_path=%s marks_path=%s',
        len(open_trades), len(marks), ok, failed, summary_path, marks_path,
    )
    return summary
