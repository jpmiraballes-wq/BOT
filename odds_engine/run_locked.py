from __future__ import annotations

import fcntl
import logging
import os
import sys

from main import run_once, _log_to_base44
import paper_mark
import paper_portfolio_summary

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('odds_engine')

LOCK_PATH = '/tmp/jp_odds_engine.lock'


def main() -> int:
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning('previous odds_engine run still active; skipping this launchd interval')
            return 0

        try:
            run_once()

            # Best-effort paper PnL mark-to-market.
            # Never fail the engine because of PnL marking.
            try:
                paper_mark.main()
            except Exception as mark_exc:
                log.exception('paper mark failed: %s', mark_exc)
                _log_to_base44('error', 'paper_mark_failed', {'error': str(mark_exc)})

            # Best-effort paper portfolio summary.
            # Never fail the engine because of portfolio reporting.
            try:
                paper_portfolio_summary.main()
            except Exception as summary_exc:
                log.exception('paper portfolio summary failed: %s', summary_exc)
                _log_to_base44('error', 'paper_portfolio_summary_failed', {'error': str(summary_exc)})

            return 0
        except Exception as exc:
            log.exception('run failed: %s', exc)
            _log_to_base44('error', 'run_failed', {'error': str(exc)})
            return 1
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
