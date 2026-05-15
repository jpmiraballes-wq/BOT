from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from config import settings
from models import now_iso

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('paper_mark')

DATA_DIR = settings.data_dir
PAPER_PATH = DATA_DIR / 'papertrade.jsonl'
MARKS_PATH = DATA_DIR / 'papertrade_marks.jsonl'


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
    return rows


def _open_unique_trades() -> list[dict[str, Any]]:
    rows = _load_jsonl(PAPER_PATH)
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    dup_count: dict[tuple[str, str, str], int] = {}

    for row in rows:
        if row.get('status') != 'open':
            continue
        key = (
            str(row.get('external_event_id') or ''),
            str(row.get('token_id') or ''),
            str(row.get('side') or 'YES'),
        )
        if not key[0] or not key[1]:
            continue

        dup_count[key] = dup_count.get(key, 0) + 1

        # Mantener la primera posición abierta real.
        # Los duplicados viejos no se cuentan varias veces.
        if key not in by_key:
            by_key[key] = row

    out = []
    for key, row in by_key.items():
        row = dict(row)
        row['_duplicate_open_rows'] = dup_count.get(key, 1)
        out.append(row)
    return out


def _extract_price(payload: Any) -> float | None:
    if isinstance(payload, dict):
        for k in ('price', 'mid', 'value'):
            v = payload.get(k)
            try:
                if v is not None:
                    p = float(v)
                    if 0 < p < 1:
                        return p
            except Exception:
                pass
    try:
        p = float(payload)
        if 0 < p < 1:
            return p
    except Exception:
        pass
    return None


def _clob_price(token_id: str, side: str = 'SELL') -> float | None:
    url = settings.polymarket_clob_url.rstrip('/') + '/price'
    try:
        resp = requests.get(url, params={'token_id': token_id, 'side': side}, timeout=12)
        if resp.status_code >= 400:
            log.warning('clob_price_failed status=%s body=%s', resp.status_code, resp.text[:200])
            return None
        return _extract_price(resp.json())
    except Exception as exc:
        log.warning('clob_price_exception token=%s side=%s error=%s', token_id[:10], side, exc)
        return None


def _mark_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    token_id = str(trade.get('token_id') or '')
    if not token_id:
        return None

    # Para una posición YES comprada, mark-to-market conservador = precio al que podrías vender.
    mark_price = _clob_price(token_id, side='SELL')

    # Fallback si no hay bid/SELL: usar BUY, pero marcarlo como fallback.
    price_source = 'clob_sell'
    if mark_price is None:
        mark_price = _clob_price(token_id, side='BUY')
        price_source = 'clob_buy_fallback'

    if mark_price is None:
        return {
            'trade_id': trade.get('id'),
            'signal_id': trade.get('signal_id'),
            'external_event_id': trade.get('external_event_id'),
            'polymarket_market_id': trade.get('polymarket_market_id'),
            'token_id': token_id,
            'side': trade.get('side') or 'YES',
            'status': 'mark_failed',
            'entry_price': trade.get('entry_price'),
            'mark_price': None,
            'price_source': 'none',
            'duplicate_open_rows': trade.get('_duplicate_open_rows', 1),
            'marked_at': now_iso(),
        }

    entry = float(trade.get('entry_price') or 0.0)
    qty = float(trade.get('quantity') or 0.0)
    size = float(trade.get('size_usd') or 0.0)

    pnl_usd = (mark_price - entry) * qty
    pnl_pct = ((mark_price - entry) / entry * 100.0) if entry > 0 else 0.0

    return {
        'trade_id': trade.get('id'),
        'signal_id': trade.get('signal_id'),
        'external_event_id': trade.get('external_event_id'),
        'polymarket_market_id': trade.get('polymarket_market_id'),
        'token_id': token_id,
        'side': trade.get('side') or 'YES',
        'status': 'open_marked',
        'entry_price': round(entry, 6),
        'mark_price': round(mark_price, 6),
        'price_source': price_source,
        'size_usd': round(size, 6),
        'quantity': round(qty, 6),
        'unrealized_pnl_usd': round(pnl_usd, 6),
        'unrealized_pnl_pct': round(pnl_pct, 4),
        'duplicate_open_rows': trade.get('_duplicate_open_rows', 1),
        'marked_at': now_iso(),
    }


def main() -> None:
    trades = _open_unique_trades()
    marks = []

    for trade in trades:
        mark = _mark_trade(trade)
        if mark:
            marks.append(mark)

    MARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MARKS_PATH.open('a', encoding='utf-8') as f:
        for mark in marks:
            f.write(json.dumps(mark, ensure_ascii=False, default=str) + '\n')

    ok = sum(1 for m in marks if m.get('status') == 'open_marked')
    failed = sum(1 for m in marks if m.get('status') == 'mark_failed')

    log.info(
        'paper_mark_complete open_unique=%s marks=%s ok=%s failed=%s path=%s',
        len(trades), len(marks), ok, failed, MARKS_PATH,
    )

    print(json.dumps({
        'open_unique_trades': len(trades),
        'marks_created': len(marks),
        'ok': ok,
        'failed': failed,
        'marks_path': str(MARKS_PATH),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
