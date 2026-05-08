from __future__ import annotations

import fcntl
import logging
import os
import sys

from main import run_once, _log_to_base44

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
