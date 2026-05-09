from __future__ import annotations

import json
import logging

from paper_maker_live_shadow import run_live_shadow_audit


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = run_live_shadow_audit()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
