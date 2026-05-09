from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from config import settings
from paper_maker_top_shadow import run_top_near_shadow_cycle


FILES_TO_ARCHIVE = [
    'paper_maker_summary.json',
    'paper_maker_markout.json',
    'paper_maker_execution_audit.json',
    'paper_maker_queue_audit.json',
    'paper_maker_live_shadow_summary.json',
    'paper_maker_quotes.jsonl',
    'paper_maker_fills.jsonl',
    'paper_maker_orders.jsonl',
    'paper_maker_cancels.jsonl',
    'paper_maker_live_shadow_runs.jsonl',
]


def archive_current_sample() -> str:
    data_dir = Path(settings.data_dir)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_dir = data_dir / f'archive_v9_1_clean_{stamp}'
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for name in FILES_TO_ARCHIVE:
        src = data_dir / name
        if src.exists():
            shutil.move(str(src), str(archive_dir / name))
            moved.append(name)
    manifest = {
        'created_at': datetime.now().isoformat(),
        'reason': 'clean V9.1 top-near one-tick-behind live-shadow sample',
        'moved': moved,
    }
    (archive_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return str(archive_dir)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    archive_dir = archive_current_sample()
    result = run_top_near_shadow_cycle()
    print(json.dumps({'archived_to': archive_dir, 'result': result}, ensure_ascii=False, indent=2, default=str))
