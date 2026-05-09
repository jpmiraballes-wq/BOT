from __future__ import annotations

import json
import logging

from paper_maker_god import run_paper_maker_once
from paper_maker_execution_audit import run_execution_audit
from paper_maker_live_shadow import run_live_shadow_audit


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    maker = run_paper_maker_once()
    execution = run_execution_audit()
    shadow = run_live_shadow_audit()
    result = {
        'maker': {
            'orders_simulated_today': maker.get('orders_simulated_today'),
            'fills_simulated_today': maker.get('fills_simulated_today'),
            'open_quotes': maker.get('open_quotes'),
            'maker_total_pnl_usd': maker.get('maker_total_pnl_usd'),
            'inventory_exposure_usd': maker.get('inventory_exposure_usd'),
            'strict_fill_rejects': maker.get('strict_fill_rejects'),
        },
        'execution': {
            'verdict': execution.get('verdict'),
            'execution_ok_rate': execution.get('execution_ok_rate'),
            'avg_execution_risk_score': execution.get('avg_execution_risk_score'),
        },
        'live_shadow_v9': {
            'verdict': shadow.get('verdict'),
            'shadow_fills': shadow.get('shadow_fills'),
            'at_top_rate': shadow.get('at_top_rate'),
            'near_top_rate': shadow.get('near_top_rate'),
            'queue_adjusted_shadow_pnl_usd': shadow.get('queue_adjusted_shadow_pnl_usd'),
            'queue_adjusted_roi_on_rotated_notional': shadow.get('queue_adjusted_roi_on_rotated_notional'),
            'warnings': shadow.get('warnings'),
        },
        'official_paper_metric': 'live_shadow_v9.queue_adjusted_shadow_pnl_usd',
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
