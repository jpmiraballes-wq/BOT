"""main.py - Loop principal del bot de Polymarket (v3.1 multi-estrategia).

Cambios v3.1:
  - Integra umbrella_executor: tras detectar oportunidades de logical_arb,
    ejecuta hasta 2 umbrella_over_children por ciclo con ordenes pseudo-FOK.
  - Resto identico a v3.0.
"""

import logging
import logging.handlers
import signal
import sys
import time
import traceback
from typing import Any, Dict, List

from capital_allocator import CapitalAllocator
from circuit_breakers import CircuitBreakers
from config import (
    CAPITAL_USDC, DRY_RUN, LOG_PATH, MAIN_LOOP_INTERVAL_SECONDS,
    SHUTDOWN_FLAG_PATH, validate_config,
)
from paper_broker import PaperBroker
from paper_daily_report import PaperDailyReporter
from decision_logger import log_decision, log_warning
from kelly import KellySizer
from logical_arb import scan_logical_arb
from news_trading import NewsTrader
from stat_arb import StatArb
from resolution_snipe import ResolutionSniper
from market_scanner import scan_markets
from order_manager import OrderManager
from reporter import Reporter
from risk_manager import RiskManager
from bot_config_reader import fetch_bot_config  # dashboard control
from strategies.umbrella_executor import run_umbrella_cycle
from portfolio_sync import PortfolioSync
from auto_close import AutoClose

AUTO_CLOSE_EVERY_N_ITERATIONS = 2

PORTFOLIO_SYNC_EVERY_N_ITERATIONS = 2


MM_STRATEGY = "market_making"
ARB_STRATEGY = "logical_arb"


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


_TRADE_STATS_CACHE = {"ts": 0.0, "data": None}
_TRADE_STATS_TTL = 60.0


def _trade_stats():
    """Calcula daily_pnl/total_pnl/win_rate/total_trades desde tabla Trade.

    Cache de 60s para no pegarle a Base44 en cada heartbeat.
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    now = _time.time()
    if _TRADE_STATS_CACHE["data"] is not None and (now - _TRADE_STATS_CACHE["ts"]) < _TRADE_STATS_TTL:
        return _TRADE_STATS_CACHE["data"]

    try:
        from base44_client import list_records
        trades = list_records("Trade", sort="-created_date", limit=1000) or []
    except Exception as _exc:
        logger.warning("trade_stats fallo: %s", _exc)
        trades = []

    today = _dt.now(_tz.utc).date()
    total_pnl = 0.0
    daily_pnl = 0.0
    wins = 0
    total_closed = 0

    for t in trades:
        if t.get("status") != "closed":
            continue
        try:
            pnl = float(t.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        total_pnl += pnl
        total_closed += 1
        if pnl > 0:
            wins += 1
        exit_time = t.get("exit_time") or t.get("entry_time")
        if exit_time:
            try:
                d = _dt.fromisoformat(exit_time.replace("Z", "+00:00")).date()
                if d == today:
                    daily_pnl += pnl
            except (ValueError, AttributeError):
                pass

    data = {
        "daily_pnl": round(daily_pnl, 4),
        "total_pnl": round(total_pnl, 4),
        "win_rate": round((wins / total_closed) * 100.0, 2) if total_closed else 0.0,
        "total_trades": total_closed,
    }
    _TRADE_STATS_CACHE["ts"] = now
    _TRADE_STATS_CACHE["data"] = data
    return data


def build_snapshot(mode, rm, om, notes=""):
    open_orders = om.get_open_orders() if om.client else []
    deployed = compute_capital_deployed(open_orders)
    stats = _trade_stats()
    return {
        "mode": mode,
        "capital_total": rm.current_equity or CAPITAL_USDC,
        "capital_deployed": deployed,
        "daily_pnl": stats["daily_pnl"],
        "total_pnl": stats["total_pnl"],
        "win_rate": stats["win_rate"],
        "total_trades": stats["total_trades"],
        "drawdown_pct": rm.drawdown_pct,
        "open_positions": len({o.get("market") or o.get("market_id")
                               for o in open_orders if o}),
        "notes": notes,
    }


def build_size_fn(sizer, rm, cb, deployed_fn, budget_fn):
    def _size(opp):
        mid = float(opp.get("mid") or 0.0)
        edge = float(opp.get("spread_pct") or 0.0) / 2.0
        sizer.record_tick(opp["market_id"], mid)

        rm_capital = rm.deployable_capital(deployed_fn())
        strategy_budget = budget_fn()
        capital_available = min(rm_capital, strategy_budget)
        if capital_available <= 0:
            return 0.0

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
    logger.info("Polymarket Multi-Strategy Bot v3.1 - umbrella executor activo")
    logger.info("=" * 60)

    try:
        (None if DRY_RUN else validate_config())
    except EnvironmentError as exc:
        logger.critical(str(exc))
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rm = RiskManager()
    reporter = Reporter()
    if DRY_RUN:
        logger.info("=== MODO PAPER TRADING ACTIVO (DRY_RUN=true) ===")
        om = PaperBroker()
        paper_reporter = PaperDailyReporter(om)
    else:
        om = OrderManager()
        paper_reporter = None
    sizer = KellySizer()
    cb = CircuitBreakers()
    allocator = CapitalAllocator()

    mm_allocated = allocator.get_allocated(MM_STRATEGY)
    arb_allocated = allocator.get_allocated(ARB_STRATEGY)
    logger.info("MM allocated=%.2f USDC | ArbLogic allocated=%.2f USDC",
                mm_allocated, arb_allocated)

    try:
        om.connect()
    except Exception as exc:
        logger.critical("No se pudo inicializar OrderManager: %s", exc)
        logger.debug(traceback.format_exc())
        reporter.report(build_snapshot("error", rm, om, notes=str(exc)), force=True)
        return 3

    # ----- NewsTrader (Fase 2) -----
    try:
        news_trader = NewsTrader(om, allocator)
        logger.info("NewsTrader activo.")
    except Exception as _exc:
        logger.error("No se pudo inicializar NewsTrader: %s", _exc)
        news_trader = None

    # ----- StatArb (Fase 3) -----
    try:
        stat_arb = StatArb(om, allocator)
        logger.info("StatArb activo.")
    except Exception as _exc:
        logger.error("No se pudo inicializar StatArb: %s", _exc)
        stat_arb = None

    # ----- ResolutionSniper (Fase 4) -----
    try:
        res_sniper = ResolutionSniper(om, allocator)
        logger.info("ResolutionSniper activo.")
    except Exception as _exc:
        logger.error("No se pudo inicializar ResolutionSniper: %s", _exc)
        res_sniper = None

    reporter.report(build_snapshot("running", rm, om), force=True)

    def _current_deployed():
        return compute_capital_deployed(om.get_open_orders())

    def _mm_budget():
        return allocator.get_available(MM_STRATEGY)

    size_fn = build_size_fn(sizer, rm, cb, _current_deployed, _mm_budget)

    # PortfolioSync: actualiza current_price y PnL unrealized de posiciones.
    try:
        psync = PortfolioSync(om.client)
        logger.info("PortfolioSync activo (cada %d iteraciones).",
                    PORTFOLIO_SYNC_EVERY_N_ITERATIONS)
    except Exception as _exc:
        logger.warning("No se pudo inicializar PortfolioSync: %s", _exc)
        psync = None

    try:
        aclose = AutoClose(om)
        logger.info("AutoClose activo (cada %d iteraciones).",
                    AUTO_CLOSE_EVERY_N_ITERATIONS)
    except Exception as _exc:
        logger.warning("No se pudo inicializar AutoClose: %s", _exc)
        aclose = None

    iteration = 0
    while not _stop_requested:
        iteration += 1
        loop_started = time.time()
        # ---- Control remoto desde dashboard (BotConfig) ----
        try:
            bot_cfg = fetch_bot_config() or {}
        except Exception as _cfg_exc:
            logger.warning("fetch_bot_config fallo: %s", _cfg_exc)
            bot_cfg = {}
        if iteration == 1 or iteration % 5 == 0:
            logger.info(
                "BotConfig leido: paused=%s emergency_stop=%s mode=%s id=%s",
                bool(bot_cfg.get("paused")),
                bool(bot_cfg.get("emergency_stop")),
                bot_cfg.get("mode"),
                bot_cfg.get("id"),
            )
        if bot_cfg.get("emergency_stop"):
            logger.critical("EMERGENCY STOP desde dashboard (at=%s). Cancelando todo y saliendo.",
                            bot_cfg.get("emergency_stop_at"))
            try:
                om.cancel_all()
            except Exception as _e:
                logger.error("cancel_all en emergency stop fallo: %s", _e)
            _close = getattr(om, "close_all_positions", None)
            if callable(_close):
                try:
                    _close()
                except Exception as _e:
                    logger.error("close_all_positions en emergency stop fallo: %s", _e)
            reporter.report(build_snapshot("stopped", rm, om, notes="emergency_stop_dashboard"),
                            force=True)
            break
        if bot_cfg.get("paused"):
            logger.info("Bot pausado desde dashboard. Solo mantenimiento.")
            try:
                om.cancel_stale_orders()
            except Exception as _e:
                logger.error("cancel_stale_orders en pausa fallo: %s", _e)
            reporter.report(build_snapshot("paused", rm, om, notes="paused_dashboard"))
            elapsed = time.time() - loop_started
            time.sleep(max(1.0, MAIN_LOOP_INTERVAL_SECONDS - elapsed))
            continue
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
                om.close_profitable_positions()
                reporter.report(build_snapshot(
                    "paused", rm, om,
                    notes="intraday_dd_pause (%ds left)" % remaining,
                ))
                elapsed = time.time() - loop_started
                time.sleep(max(1.0, MAIN_LOOP_INTERVAL_SECONDS - elapsed))
                continue

            om.close_profitable_positions()

            # ---- MARKET MAKING ----
            mm_enabled = allocator.is_enabled(MM_STRATEGY)
            mm_budget = allocator.get_available(MM_STRATEGY)

            if not mm_enabled:
                logger.info("MM pausado desde dashboard. Solo mantenimiento.")
                om.cancel_stale_orders()
            elif mm_budget <= 0:
                logger.info("MM sin capital disponible (deployed=%.2f / allocated=%.2f).",
                            allocator.get_deployed(MM_STRATEGY),
                            allocator.get_allocated(MM_STRATEGY))
                om.cancel_stale_orders()
            else:
                opportunities = scan_markets()
                opportunities = [o for o in opportunities if cb.filter_opportunity(o)]
                logger.info("MM oportunidades tras filtros: %d (budget=%.2f)",
                            len(opportunities), mm_budget)

                deployed = _current_deployed()
                ok, msg = rm.enforce_exposure_cap(deployed)
                if not ok:
                    logger.warning("Cap de exposicion superado.")
                    om.cancel_all()
                    reporter.report(build_snapshot("paused", rm, om, notes=msg), force=True)
                    if paper_reporter is not None:
                        try:
                            paper_reporter.tick()
                            if paper_reporter.should_stop():
                                logger.info("[PAPER] duracion alcanzada, saliendo.")
                                break
                        except Exception as _exc:
                            logger.error("paper_reporter tick fallo: %s", _exc)

                    time.sleep(MAIN_LOOP_INTERVAL_SECONDS)
                    continue

                if rm.can_open_new_position(deployed):
                    om.refresh(opportunities, size_fn)
                else:
                    logger.info("Sin capital desplegable (rm); solo mantenimiento.")
                    om.cancel_stale_orders()

            mm_deployed = _current_deployed()
            allocator.report_deployed(MM_STRATEGY, mm_deployed)

            # ---- LOGICAL ARB (detection + umbrella execution) ----
            if news_trader is not None:
                try:
                    news_trader.run_cycle()
                except Exception as _exc:
                    logger.error("news_trading fallo: %s", _exc)

            if stat_arb is not None:
                try:
                    stat_arb.run_cycle()
                except Exception as _exc:
                    logger.error("stat_arb fallo: %s", _exc)

            if res_sniper is not None:
                try:
                    res_sniper.run_cycle()
                except Exception as _exc:
                    logger.error("resolution_snipe fallo: %s", _exc)

            try:
                arb_opps = scan_logical_arb()
                if arb_opps:
                    log_decision(
                        reason="logical_arb_detected",
                        market="%d signals" % len(arb_opps),
                        strategy=ARB_STRATEGY,
                        extra={"top": arb_opps[:3]},
                    )
                    # Solo ejecuta umbrella. binary_under y monotonic quedan
                    # como deteccion hasta tener executor dedicado.
                    if allocator.is_enabled(ARB_STRATEGY):
                        results = run_umbrella_cycle(om, arb_opps, max_per_cycle=2)
                        for r in results:
                            if r.get("status") == "executed":
                                logger.info("Umbrella arb ejecutado: %s", r)
                            elif r.get("status") == "rolled_back":
                                logger.warning("Umbrella rollback: %s", r)
            except Exception as exc:
                logger.error("logical_arb cycle fallo: %s", exc)
                logger.debug(traceback.format_exc())

            if psync is not None and iteration % PORTFOLIO_SYNC_EVERY_N_ITERATIONS == 0:
                try:
                    psync.sync()
                except Exception as _exc:
                    logger.warning("portfolio_sync fallo: %s", _exc)

            # Heartbeat ANTES de AutoClose para no quedar "offline" si el
            # cierre masivo tarda. Si AutoClose cierra muchas, el siguiente
            # ciclo emitira otro heartbeat.
            reporter.report(build_snapshot("running", rm, om))

            if aclose is not None and iteration % AUTO_CLOSE_EVERY_N_ITERATIONS == 0:
                try:
                    aclose.run()
                except Exception as _exc:
                    logger.warning("auto_close fallo: %s", _exc)

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
