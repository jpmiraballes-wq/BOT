from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _iter_jsonl(path: Path, limit: int = 5000):
    if not path.exists():
        return []
    rows = []
    try:
        lines = path.read_text(errors='ignore').splitlines()[-limit:]
        for line in lines:
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        return []
    return rows


def _score_from_threshold(value: float, good: float, bad: float, higher_is_better: bool = True) -> float:
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


def audit_paper_maker() -> dict:
    data_dir = Path(settings.data_dir)
    summary_path = data_dir / 'paper_maker_summary.json'
    fills_path = data_dir / 'paper_maker_fills.jsonl'
    quotes_path = data_dir / 'paper_maker_quotes.jsonl'
    runs_path = data_dir / 'paper_maker_runs.jsonl'

    summary = _load_json(summary_path)
    fills = _iter_jsonl(fills_path, limit=10000)
    quotes = _iter_jsonl(quotes_path, limit=10000)
    runs = _iter_jsonl(runs_path, limit=1000)

    orders_today = int(_float(summary.get('orders_simulated_today')))
    fills_today = int(_float(summary.get('fills_simulated_today')))
    cancels_today = int(_float(summary.get('cancels_simulated_today')))
    open_quotes = int(_float(summary.get('open_quotes')))
    inventory_exposure = _float(summary.get('inventory_exposure_usd'))
    bankroll = _float(summary.get('paper_bankroll_usd'), 1_000_000.0)
    raw_pnl = _float(summary.get('maker_total_pnl_usd'))
    realized = _float(summary.get('realized_spread_pnl_usd'))
    unrealized = _float(summary.get('unrealized_inventory_pnl_usd'))
    rewards = _float(summary.get('estimated_rewards_usd'))

    fill_rate = fills_today / max(1, orders_today)
    cancel_ratio = cancels_today / max(1, orders_today)
    inventory_ratio = inventory_exposure / max(1.0, bankroll)
    raw_pnl_on_inventory_pct = 100.0 * raw_pnl / max(1.0, inventory_exposure)
    unrealized_share = abs(unrealized) / max(1.0, abs(raw_pnl)) if raw_pnl else 0.0

    by_token_notional = defaultdict(float)
    by_market_notional = defaultdict(float)
    side_counts = Counter()
    for f in fills:
        notional = _float(f.get('notional_usd'))
        by_token_notional[str(f.get('token_id_short') or f.get('token_id') or '')] += notional
        by_market_notional[str(f.get('market_id') or '')] += notional
        side_counts[str(f.get('side') or '').upper()] += 1

    total_fill_notional = sum(by_token_notional.values())
    top_token_concentration = max(by_token_notional.values() or [0.0]) / max(1.0, total_fill_notional)
    top_market_concentration = max(by_market_notional.values() or [0.0]) / max(1.0, total_fill_notional)

    quote_side_counts = Counter(str(q.get('side') or '').upper() for q in quotes)
    buy_quotes = quote_side_counts.get('BUY', 0)
    sell_quotes = quote_side_counts.get('SELL', 0)
    side_balance = min(buy_quotes, sell_quotes) / max(1, max(buy_quotes, sell_quotes)) if max(buy_quotes, sell_quotes) else 0.0

    throughput_score = _score_from_threshold(orders_today, good=5000, bad=500, higher_is_better=True)
    fill_realism_score = _score_from_threshold(fill_rate, good=0.08, bad=0.35, higher_is_better=False)
    cancel_realism_score = _score_from_threshold(cancel_ratio, good=0.40, bad=0.05, higher_is_better=True)
    inventory_score = _score_from_threshold(inventory_ratio, good=0.005, bad=0.05, higher_is_better=False)
    concentration_score = _score_from_threshold(top_token_concentration, good=0.18, bad=0.65, higher_is_better=False)
    side_balance_score = _score_from_threshold(side_balance, good=0.35, bad=0.05, higher_is_better=True)
    pnl_quality_score = _score_from_threshold(unrealized_share, good=0.35, bad=0.85, higher_is_better=False)

    simulation_quality_score = round(
        0.20 * throughput_score
        + 0.20 * fill_realism_score
        + 0.15 * cancel_realism_score
        + 0.15 * inventory_score
        + 0.10 * concentration_score
        + 0.10 * side_balance_score
        + 0.10 * pnl_quality_score,
        2,
    )

    # Conservative PnL: punish fake fill optimism, inventory mark-to-mid, concentration and low side-balance.
    haircut = 1.0
    haircut *= max(0.15, min(1.0, fill_realism_score / 100.0))
    haircut *= max(0.25, min(1.0, inventory_score / 100.0))
    haircut *= max(0.30, min(1.0, concentration_score / 100.0))
    haircut *= max(0.50, min(1.0, side_balance_score / 100.0))
    conservative_realized = realized * max(0.25, min(1.0, fill_realism_score / 100.0))
    conservative_unrealized = unrealized * 0.25 * max(0.25, min(1.0, inventory_score / 100.0))
    conservative_rewards = rewards * 0.25
    conservative_pnl = conservative_realized + conservative_unrealized + conservative_rewards
    conservative_pnl *= max(0.25, haircut)

    warnings = []
    if fill_rate > 0.35:
        warnings.append('fill_rate_too_high_for_realistic_maker_sim')
    if inventory_ratio > 0.03:
        warnings.append('inventory_exposure_high_vs_bankroll')
    if top_token_concentration > 0.50:
        warnings.append('too_much_fill_concentration_in_one_token')
    if side_balance < 0.15:
        warnings.append('quote_side_imbalance_buy_sell')
    if unrealized_share > 0.75:
        warnings.append('pnl_depends_too_much_on_unrealized_inventory')

    if simulation_quality_score >= 75 and conservative_pnl > 0 and not warnings:
        verdict = 'GOOD_PAPER_SIGNAL'
    elif simulation_quality_score >= 55 and conservative_pnl > 0:
        verdict = 'PROMISING_BUT_NEEDS_MORE_HOURS'
    else:
        verdict = 'NOT_TRUSTWORTHY_YET'

    audit = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'source_summary_generated_at': summary.get('generated_at'),
        'verdict': verdict,
        'simulation_quality_score': simulation_quality_score,
        'raw_maker_total_pnl_usd': round(raw_pnl, 6),
        'conservative_maker_pnl_usd': round(conservative_pnl, 6),
        'realized_spread_pnl_usd': round(realized, 6),
        'unrealized_inventory_pnl_usd': round(unrealized, 6),
        'estimated_rewards_usd': round(rewards, 6),
        'orders_today': orders_today,
        'fills_today': fills_today,
        'cancels_today': cancels_today,
        'open_quotes': open_quotes,
        'fill_rate': round(fill_rate, 6),
        'cancel_ratio': round(cancel_ratio, 6),
        'inventory_exposure_usd': round(inventory_exposure, 6),
        'inventory_ratio_of_bankroll': round(inventory_ratio, 8),
        'raw_pnl_on_inventory_pct': round(raw_pnl_on_inventory_pct, 4),
        'unrealized_share_of_raw_pnl': round(unrealized_share, 6),
        'top_token_fill_concentration': round(top_token_concentration, 6),
        'top_market_fill_concentration': round(top_market_concentration, 6),
        'quote_side_balance': round(side_balance, 6),
        'scores': {
            'throughput_score': round(throughput_score, 2),
            'fill_realism_score': round(fill_realism_score, 2),
            'cancel_realism_score': round(cancel_realism_score, 2),
            'inventory_score': round(inventory_score, 2),
            'concentration_score': round(concentration_score, 2),
            'side_balance_score': round(side_balance_score, 2),
            'pnl_quality_score': round(pnl_quality_score, 2),
        },
        'warnings': warnings,
        'top_fill_tokens': sorted(
            [{'token': k, 'notional_usd': round(v, 6)} for k, v in by_token_notional.items() if k],
            key=lambda x: x['notional_usd'],
            reverse=True,
        )[:10],
        'paper_only_note': 'Auditor uses real Polymarket-derived paper maker logs, but applies conservative haircuts to avoid trusting optimistic simulated fills.',
    }

    out_path = data_dir / 'paper_maker_audit.json'
    out_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    return audit


if __name__ == '__main__':
    print(json.dumps(audit_paper_maker(), ensure_ascii=False, indent=2, default=str))
