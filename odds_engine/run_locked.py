from __future__ import annotations

import fcntl
import logging
import os
import sys

from main import run_once, _log_to_base44

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('odds_engine')

LOCK_PATH = '/tmp/jp_odds_engine.lock'


def _run_execution_audit_safely() -> None:
    """Run V7 execution reality audit without risking the main service.

    This is paper-only. It only reads local paper maker files and public CLOB
    books, then writes MakerExecutionAudit/local JSON. It must never prevent the
    main odds engine cycle from completing.
    """
    try:
        from paper_maker_execution_audit import run_execution_audit
        audit = run_execution_audit()
        log.info(
            'execution_audit_after_cycle verdict=%s quotes=%s ok_rate=%s avg_risk=%s strict_fills=%s strict_exec=%s',
            audit.get('verdict'),
            audit.get('quotes_measured'),
            audit.get('execution_ok_rate'),
            audit.get('avg_execution_risk_score'),
            audit.get('strict_measured_fills'),
            audit.get('strict_executable_markout_usd'),
        )
    except Exception as exc:
        log.warning('execution_audit_after_cycle_failed err=%s', exc)
        try:
            _log_to_base44('warning', 'execution_audit_after_cycle_failed', {'error': str(exc)})
        except Exception:
            pass


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
            _run_execution_audit_safely()
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
