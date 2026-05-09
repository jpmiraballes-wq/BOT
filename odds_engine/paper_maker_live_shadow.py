from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from config import settings
from paper_maker_engine import _float, _now_iso

log = logging.getLogger('paper_maker_live_shadow')


def _parse_ts(value: Any) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0.0


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_jsonl_tail(path: Path, limit: int = 2500) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(errors='ignore').splitlines()[-limit:]
    except Exception:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


class LiveShadowPaperAudit:
    """V9 audit-only skeleton.

    No live calls. No signing. No order posting. This first step only builds a
    durable V9 summary from existing paper maker quotes/fills and the current
    queue audit, so the dashboard/terminal can switch away from synthetic fill
    PnL as the official metric.
    """

    def __init__(self) -> None:
        self.data_dir = Path(settings.data_dir)
        self.summary_path = self.data_dir / 'paper_maker_summary.json'
        self.markout_path = self.data_dir / 'paper_maker_markout.json'
        self.queue_path = self.data_dir / 'paper_maker_queue_audit.json'
        self.execution_path = self.data_dir / 'paper_maker_execution_audit.json'
        self.quotes_path = self.data_dir / 'paper_maker_quotes.jsonl'
        self.fills_path = self.data_dir / 'paper_maker_fills.jsonl'
        self.out_path = self.data_dir / 'paper_maker_live_shadow_summary.json'
        self.runs_path = self.data_dir / 'paper_maker_live_shadow_runs.jsonl'

    def run(self) -> dict:
        summary = _read_json(self.summary_path)
        markout = _read_json(self.markout_path)
        queue = _read_json(self.queue_path)
        execution = _read_json(self.execution_path)
        quotes = _read_jsonl_tail(self.quotes_path, 2500)
        fills = _read_jsonl_tail(self.fills_path, 2500)

        now = time.time()
        quote_ages = [max(0.0, now - _parse_ts(q.get('created_at'))) for q in quotes if q.get('created_at')]
        avg_quote_age = sum(quote_ages) / max(1, len(quote_ages))
        stale_quote_rate = sum(1 for x in quote_ages if x > 180) / max(1, len(quote_ages))

        q_adj = _float(queue.get('queue_adjusted_executable_markout_usd'))
        rotated = _float(markout.get('total_notional_usd'))
        roi = (100.0 * q_adj / rotated) if rotated else 0.0
        measured = int(markout.get('measured_fills') or 0)
        queue_fill_rows = int(queue.get('fills_measured_for_queue') or 0)
        near_proxy = max(0.0, 1.0 - _float(queue.get('fill_queue_risk_rate'), 1.0))

        warnings: list[str] = []
        if measured < 30:
            warnings.append('needs_minimum_30_measured_fills')
        if queue_fill_rows < 30:
            warnings.append('needs_minimum_30_queue_measured_fills')
        if q_adj <= 0:
            warnings.append('queue_adjusted_pnl_not_positive_yet')
        if _float(queue.get('fill_queue_risk_rate')) > 0.50:
            warnings.append('high_fill_queue_risk_rate')

        verdict = 'SHADOW_NEEDS_MORE_DATA'
        if measured >= 30 and queue_fill_rows >= 30:
            if q_adj <= 0 or _float(queue.get('fill_queue_risk_rate')) > 0.75:
                verdict = 'SHADOW_NOT_REALISTIC'
            elif q_adj > 0 and queue_fill_rows < 100:
                verdict = 'SHADOW_WORKING_LOW_VOLUME'
            else:
                verdict = 'SHADOW_WORKING'
        if measured >= 300 and queue_fill_rows >= 300 and q_adj > 0 and roi > 0 and _float(queue.get('fill_queue_risk_rate')) < 0.35:
            verdict = 'SHADOW_READY_FOR_TINY_LIVE_REVIEW'

        audit = {
            'generated_at': _now_iso(),
            'mode': 'LIVE_SHADOW_PAPER_V9_SKELETON',
            'paper_only': True,
            'live_orders_enabled': False,
            'verdict': verdict,
            'virtual_orders_created': 0,
            'virtual_orders_open': int(summary.get('open_quotes') or 0),
            'virtual_orders_cancelled': 0,
            'virtual_orders_reposted': 0,
            'shadow_fills': queue_fill_rows,
            'shadow_fill_rate': round(queue_fill_rows / max(1, len(quotes)), 6),
            'at_top_rate': _float(queue.get('fill_at_top_or_better_rate')),
            'near_top_rate': round(near_proxy, 6),
            'avg_quote_lifetime_seconds': round(avg_quote_age, 4),
            'stale_quote_rate': round(stale_quote_rate, 6),
            'avg_queue_ahead_notional_usd': _float(queue.get('fill_avg_queue_ahead_notional_usd')),
            'avg_expected_fill_probability': _float(queue.get('fill_avg_visible_partial_fill_rate_estimate')),
            'raw_shadow_pnl_usd': _float(summary.get('maker_total_pnl_usd')),
            'executable_shadow_pnl_usd': _float(markout.get('executable_markout_usd')),
            'queue_adjusted_shadow_pnl_usd': round(q_adj, 6),
            'rotated_notional_usd': round(rotated, 6),
            'queue_adjusted_roi_on_rotated_notional': round(roi, 6),
            'warnings': warnings,
            'source_orders_simulated_today': summary.get('orders_simulated_today'),
            'source_fills_simulated_today': summary.get('fills_simulated_today'),
            'source_strict_measured_fills': measured,
            'source_strict_executable_markout_usd': _float(markout.get('executable_markout_usd')),
            'source_execution_verdict': execution.get('verdict'),
            'source_queue_verdict': queue.get('verdict'),
            'top_shadow_opportunity_tokens': queue.get('top_fill_queue_risk_tokens') or [],
            'top_shadow_problem_tokens': queue.get('top_quote_queue_risk_tokens') or [],
            'paper_only_note': 'V9 skeleton: official paper metric is queue_adjusted_shadow_pnl_usd. Full persistent virtual lifecycle comes next.',
        }
        self.out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
        with self.runs_path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(audit, ensure_ascii=False, default=str) + '\n')
        log.info(
            'paper_maker_live_shadow_v9 verdict=%s shadow_fills=%s qadj=%s roi=%s warnings=%s',
            verdict, audit['shadow_fills'], audit['queue_adjusted_shadow_pnl_usd'], audit['queue_adjusted_roi_on_rotated_notional'], warnings,
        )
        return audit


def run_live_shadow_audit() -> dict:
    return LiveShadowPaperAudit().run()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    print(json.dumps(run_live_shadow_audit(), ensure_ascii=False, indent=2, default=str))
