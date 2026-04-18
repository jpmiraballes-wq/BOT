"""
main.py - Loop principal del bot de Polymarket (v2).
"""

import logging
import logging.handlers
import signal
import sys
import time
import traceback
from typing import Any, Dict, List

from circuit_breakers import CircuitBreakers
from config import (
    CAPITAL_USDC, LOG_PATH, MAIN_LOOP_INTERVAL_SECONDS,
    SHUTDOWN_FLAG_PATH, validate_config,
)
from decision_logger import log_decision, log_warning
from kelly import KellySizer
from logical_arb import scan_logical_arb
from market_scanner import scan_markets
from order_manager import OrderManager
from reporter import Reporter
from risk_manager import RiskManager


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except PermissionError:
        root.warning("Sin permisos para %s; solo stdout.", LOG_PATH)
    except Exception as exc:
        root.warning("No se pudo abrir %s (%s); solo stdout.", LOG_PATH, exc)


logger = logging.getLogger("polymarket-bot")
_stop_requested = False


def _handle_signal(signum, _frame):
    global _stop_requested
    logger.warning("Senal %s recibida; shutdown.", signum)
    _stop_requested = True


def compute_capital_deployed(open_orders):
    total = 0.0
    for o in open_orders:
        try:
            price = float(o.get("price", 0.0))
            size = float(o.get("original_size") or o.get("size") or 0.0)
            total += price * size
        except (TypeError, ValueError):
            continue
    return total


def build_snapshot(mode, rm, om, notes=""):
    open_orders = om.get_open_orders() if om.client else []
    deployed = compute_capital_deployed(open_orders)
    return {
        "mode": mode,
        "capital_total": rm.current_equity or CAPITAL_USDC,
        "capital_deployed": deployed,
        "daily_pnl": rm.daily_pnl,
        "drawdown_pct": rm.drawdown_pct,
        "open_positions": len({o.get("market") or o.get("market_id")
                               for o in open_orders if o}),
        "notes": notes,
    }


def build_size_fn(sizer, rm, cb, deployed_fn):
    def _size(opp):
        mid = float(opp.get("mid") or 0.0)
        edge = float(opp.get("spread_pct") or 0.0) / 2.0
        sizer.record_tick(opp["market_id"], mid)
        capital_available = rm.deployable_capital(deployed_fn())
        kelly_size = sizer.compute_size(
            market_id=opp["market_id"], edge=edge,
            capital_available=capital_available, price=mid,
        )
        factor = cb.get_size_factor(mid)
        final = kelly_size * factor
        if factor < 1.0:
            log_warning(
                "size_reducido_precio_extremo",
                module="market_maker",
                extra={"market": opp.get("question"), "mid": mid,
                       "factor": factor, "size": final},
            )
        return final
    return _size


def main() -> int:
    setup_logging()
    logger.info("=" * 60)
    logger.info("Polymarket Market Maker v2 - arrancando")
    logger.info("=" * 60)

    try:
        validate_config()
    except EnvironmentError as exc:
        logger.critical(str(exc))
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rm = RiskManager()
    reporter = Reporter()
    om = OrderManager()
    sizer = KellySizer()
    cb = CircuitBreakers()

    try:
        om.connect()
    except Exception as exc:
        logger.critical("No se pudo inicializar OrderManager: %s", exc)
        logger.debug(traceback.format_exc())
        reporter.report(build_snapshot("error", rm, om, notes=str(exc)), force=True)
        return 3

    reporter.report(build_snapshot("running", rm, om), force=True)

    def _current_deployed():
        return compute_capital_deployed(om.get_open_orders())

    size_fn = build_size_fn(sizer, rm, cb, _current_deployed)

    iteration = 0
    while not _stop_requested:
        iteration += 1
        loop_started = time.time()
        try:
            if rm.is_halted() or SHUTDOWN_FLAG_PATH.exists():
                logger.critical("Halt activo (%s).", rm.halt_reason or "shutdown.flag")
                om.cancel_all()
                reporter.report(build_snapshot("stopped", rm, om, notes=rm.halt_reason), force=True)
                break

            cb.update_equity(rm.current_equity)
            if cb.is_paused():
                remaining = cb.seconds_until_resume()
                logger.warning("Pausa intradia (%ds restantes).", remaining)
                om.cancel_stale_orders()
                reporter.report(build_snapshot(
                    "paused", rm, om,
                    notes="intraday_dd_pause (%ds left)" % remaining,
                ))
                elapsed = time.time() - loop_started
                time.sleep(max(1.0, MAIN_LOOP_INTERVAL_SECONDS - elapsed))
                continue

            try:
                arb_opps = scan_logical_arb()
                if arb_opps:
                    log_decision(
                        reason="logical_arb_detected",
                        market="%d groups" % len(arb_opps),
                        strategy="logical_arb",
                        extra={"groups": [o["group_key"] for o in arb_opps][:5]},
                    )
            except Exception as exc:
                logger.error("logical_arb fallo: %s", exc)

            opportunities = scan_markets()
            opportunities = [o for o in opportunities if cb.filter_opportunity(o)]
            logger.info("Oportunidades MM tras filtros: %d", len(opportunities))

            deployed = _current_deployed()
            ok, msg = rm.enforce_exposure_cap(deployed)
            if not ok:
                logger.warning("Cap de exposicion superado.")
                om.cancel_all()
                reporter.report(build_snapshot("paused", rm, om, notes=msg), force=True)
                time.sleep(MAIN_LOOP_INTERVAL_SECONDS)
                continue

            if rm.can_open_new_position(deployed):
                om.refresh(opportunities, size_fn)
            else:
                logger.info("Sin capital desplegable; solo mantenimiento.")
                om.cancel_stale_orders()

            reporter.report(build_snapshot("running", rm, om))

        except Exception as exc:
            logger.error("Error en iteracion %d: %s", iteration, exc)
            logger.debug(traceback.format_exc())
            reporter.report(build_snapshot("error", rm, om, notes=str(exc)), force=True)

        elapsed = time.time() - loop_started
        sleep_for = max(1.0, MAIN_LOOP_INTERVAL_SECONDS - elapsed)
        slept = 0.0
        while slept < sleep_for and not _stop_requested:
            chunk = min(1.0, sleep_for - slept)
            time.sleep(chunk)
            slept += chunk

    logger.info("Shutdown: cancelando ordenes.")
    try:
        om.cancel_all()
    except Exception as exc:
        logger.error("Error cancelando: %s", exc)
    try:
        reporter.report(build_snapshot("stopped", rm, om, notes="clean shutdown"), force=True)
    except Exception as exc:
        logger.error("Error heartbeat final: %s", exc)

    logger.info("Bot detenido. Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
