"""
main.py - Loop principal del bot de Polymarket (v2).

Novedades:
  - Arbitraje logico, Kelly fraccional, circuit breakers, decision log.
  - Copy-trade drain (Positions pending_fill aprobadas por JP via Telegram).
  - TP/SL loop con fallback (frena bug dust_unsellable).
"""

import logging
import logging.handlers
import os
import signal
import sys
import time
import traceback
from typing import Any, Dict, List

# BOT_PID_LOCK_V1 — JP+Opus 2026-04-29: previene 2+ instancias en paralelo.
# Caso 29-04 ~17:30 Madrid: 4 procesos main.py corriendo simultáneo (3 huérfanos +
# 1 launchd) → 21x dupe Angels en 4min. fcntl.LOCK_EX|LOCK_NB falla inmediato si
# otro proceso ya tiene el lock → segundo intento muere con sys.exit(1).
# _lock_fh se mantiene global durante toda la vida del proceso (no GC).
import fcntl as _fcntl

_LOCK_FILE = os.path.expanduser('~/BOT/bot-v2/.bot.lock')
_lock_fh = open(_LOCK_FILE, 'w')
# PID_LOCK_GRACEFUL_RESTART_V1: al reiniciar con pkill, el proceso viejo
# puede tardar unos segundos en soltar el flock. Esperamos hasta 30s antes
# de abortar para evitar falsos exit(1) durante deploys normales.
_lock_acquired = False
for _lock_attempt in range(30):
    try:
        _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        _lock_fh.seek(0)
        _lock_fh.truncate()
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        _lock_acquired = True
        break
    except IOError:
        if _lock_attempt == 0:
            print("⏳ PID lock activo; esperando a que cierre el proceso anterior...")
        time.sleep(1)

if not _lock_acquired:
    print("❌ Bot ya está corriendo (PID lock activo en .bot.lock). Saliendo.")
    sys.exit(1)

# SHADOW_MODE_V1 — VPS Ashburn shadow mode (Bolt+Opus+JP 2026-04-27).
# Si SHADOW_MODE=true en el .env, el bot solo polea whale_watcher para medir
# lag de detección. NO toca wallet, NO crea proposals, NO ejecuta scans.
SHADOW_MODE = os.getenv("SHADOW_MODE", "false").strip().lower() == "true"
SHADOW_BOT_ID = os.getenv("SHADOW_BOT_ID", "vps-shadow")

from circuit_breakers import CircuitBreakers
from config import (
    CAPITAL_USDC, LOG_PATH, MAIN_LOOP_INTERVAL_SECONDS,
    SHUTDOWN_FLAG_PATH, validate_config,
)
from decision_logger import log_decision, log_warning
from kelly import KellySizer
from logical_arb import scan_logical_arb
from market_scanner import scan_markets
# OVERTAKE_V1_INTEGRATION — radar Pinnacle, whale watcher, twitter (Bolt+Opus 2026-04-27)
try:
    from arbitrage_radar import maybe_run_radar
except ImportError:
    maybe_run_radar = None
try:
    from whale_watcher import maybe_run_whale_watcher
except ImportError:
    maybe_run_whale_watcher = None
try:
    from apify_twitter_loop import maybe_run_twitter_loop
except ImportError:
    maybe_run_twitter_loop = None
from order_manager import OrderManager
from position_tp_sl import manage_open_positions
from reporter import Reporter
from risk_manager import RiskManager
# CAPITAL_BOOTSTRAP_V1 — lector BotConfig para sincronizar capital al arrancar.
try:
    from bot_config_reader import fetch_bot_config
except ImportError:
    fetch_bot_config = None


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


def fetch_base44_open_positions():
    """Lee Position con status=open desde Base44 y retorna (count, deployed_usdc)."""
    import urllib.request, json
    from config import BASE44_API_KEY, BASE44_APP_ID
    url = f"https://app.base44.com/api/apps/{BASE44_APP_ID}/entities/Position?limit=200&sort=-created_date"
    req = urllib.request.Request(url, headers={"api_key": BASE44_API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
            records = data if isinstance(data, list) else data.get("records", [])
            open_pos = [p for p in records if p.get("status") == "open"]
            deployed = sum(float(p.get("size_usdc") or 0) for p in open_pos)
            return len(open_pos), round(deployed, 2)
    except Exception as e:
        logger.warning("fetch_base44_open_positions_error: %s", e)  # MAIN_BASE44_FALLBACK_LOGGER_V1
        return 0, 0.0


def build_snapshot(mode, rm, om, notes=""):
    open_count, deployed = fetch_base44_open_positions()
    return {
        "mode": mode,
        "capital_total": float(rm.current_equity or 0.0),
        "capital_deployed": deployed,
        "daily_pnl": rm.daily_pnl,
        "drawdown_pct": rm.drawdown_pct,
        "open_positions": open_count,
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

    # RM_NO_FAKE_CAPITAL_V1 — leer BotConfig PRIMERO, instanciar RiskManager con capital real.
    # No mas fallback fantasma. Si BotConfig falla, el bot aborta arrancada.
    if fetch_bot_config is None:
        logger.error("ABORT: bot_config_reader no disponible. No puedo leer capital real.")
        sys.exit(1)
    try:
        cfg = fetch_bot_config(force=True) or {}
    except Exception as exc:
        logger.error("ABORT: BotConfig fetch fallo: %s. Sin capital real no arranco.", exc)
        sys.exit(1)
    live_capital = cfg.get("capital_usdc")
    if live_capital is None or float(live_capital) <= 0:
        logger.error(
            "ABORT: BotConfig.capital_usdc invalido (%s). Setealo en el dashboard.",
            live_capital,
        )
        sys.exit(1)
    try:
        rm = RiskManager(initial_capital=float(live_capital))
    except ValueError as exc:
        logger.error("ABORT: RiskManager rechazo capital: %s", exc)
        sys.exit(1)
    logger.info("Capital REAL desde BotConfig: $%.2f", float(live_capital))
    reporter = Reporter()
    om = OrderManager()
    sizer = KellySizer()
    cb = CircuitBreakers()

    if SHADOW_MODE:
        logger.warning("=" * 60)
        logger.warning("SHADOW_MODE=true (id=%s) — solo whale_watcher activo", SHADOW_BOT_ID)
        logger.warning("Skipping: om.connect, scan_markets, drain_fills, tp_sl, radar, twitter")
        logger.warning("=" * 60)
    else:
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

    # AUTO_PULL_ON_LOOP_V1 — auto-update silencioso. Cada ~60 iter (~5min con loop=5s)
    # comparamos HEAD local vs origin/main. Si hay commit nuevo, pull + exit 0.
    # run_bot.sh detecta exit 0 y relanza con código nuevo en 2s.
    import subprocess
    _AUTO_PULL_EVERY = 60  # cada 60 iteraciones del loop principal
    def _check_and_pull():
        try:
            bot_dir = os.path.dirname(os.path.abspath(__file__))
            # 1) git fetch silencioso (3s timeout)
            subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=bot_dir, timeout=10, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # 2) Comparar HEADs
            local = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=bot_dir, timeout=3
            ).decode().strip()
            remote = subprocess.check_output(
                ["git", "rev-parse", "origin/main"], cwd=bot_dir, timeout=3
            ).decode().strip()
            if local == remote:
                return False  # no hay update
            # 3) Hay commit nuevo: pull + log + exit clean
            logger.warning("AUTO_PULL: nuevo commit %s -> %s. Pulling y reiniciando.",
                           local[:8], remote[:8])
            log_warning(
                "auto_pull_triggered",
                module="main",
                extra={"local_sha": local[:12], "remote_sha": remote[:12]},
            )
            subprocess.run(
                ["git", "pull", "--ff-only", "origin", "main"],
                cwd=bot_dir, timeout=15, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True  # caller debe sys.exit(0)
        except Exception as _exc:
            logger.debug("auto_pull check fallo (%s); ignorando", _exc)
            return False

    iteration = 0
    while not _stop_requested:
        iteration += 1
        loop_started = time.time()
        try:
            # AUTO_PULL_ON_LOOP_V1 — check periódico de update.
            if iteration > 1 and iteration % _AUTO_PULL_EVERY == 0:
                if _check_and_pull():
                    # Apagado limpio antes de salir con 0
                    try:
                        om.cancel_stale_orders()
                    except Exception:
                        pass
                    try:
                        reporter.report(
                            build_snapshot("running", rm, om, notes="auto_pull_restart"),
                            force=True,
                        )
                    except Exception:
                        pass
                    logger.info("Saliendo limpio (exit=0) para que run_bot.sh relance.")
                    sys.exit(0)
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

            if SHADOW_MODE:
                # SHADOW: solo whale_watcher. Nada de wallet ni proposals.
                if maybe_run_whale_watcher is not None:
                    try:
                        maybe_run_whale_watcher()
                    except Exception as exc:
                        logger.error("whale_watcher fallo: %s", exc)
                # heartbeat liviano para ver el bot vivo en SystemState
                reporter.report(build_snapshot("running", rm, om, notes="shadow:%s" % SHADOW_BOT_ID))
                elapsed = time.time() - loop_started
                sleep_for = max(1.0, MAIN_LOOP_INTERVAL_SECONDS - elapsed)
                slept = 0.0
                while slept < sleep_for and not _stop_requested:
                    chunk = min(1.0, sleep_for - slept)
                    time.sleep(chunk)
                    slept += chunk
                continue

            # Copy-trade drain: ejecuta Positions pending_fill aprobadas por JP.
            try:
                drained = om.drain_pending_fills()
                if drained:
                    logger.info("Copy-trade: %d fills procesados", drained)
            except Exception as exc:
                logger.error("drain_pending_fills fallo: %s", exc)

            # OVERTAKE_V1 — radar Pinnacle (cada ~60s interno)
            if maybe_run_radar is not None:
                try:
                    maybe_run_radar()
                except Exception as exc:
                    logger.error("arbitrage_radar fallo: %s", exc)

            # OVERTAKE_V1 — whale watcher Tier S (cada ~30s interno)
            if maybe_run_whale_watcher is not None:
                try:
                    maybe_run_whale_watcher()
                except Exception as exc:
                    logger.error("whale_watcher fallo: %s", exc)

            # OVERTAKE_V1 — twitter loop (cada ~60s interno)
            if maybe_run_twitter_loop is not None:
                try:
                    maybe_run_twitter_loop()
                except Exception as exc:
                    logger.error("twitter_loop fallo: %s", exc)

            # TP/SL loop con fallback. Cierra Positions whale_consensus que
            # llegaron a TP/SL. Si CLOB rechaza por size <5sh, intenta precio
            # agresivo. Si todo falla, marca dust_exit (jamas deja correr la
            # perdida hasta resolucion del mercado).
            try:
                tp_sl_stats = manage_open_positions(om.client)
                if (tp_sl_stats["closed_tp"] or tp_sl_stats["closed_sl"]
                        or tp_sl_stats["dust_exits"]):
                    logger.info("TP/SL: %s", tp_sl_stats)
            except Exception as exc:
                logger.error("manage_open_positions fallo: %s", exc)

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

            # MM_FLAG_GUARD_V1 — chequea flag market_maker en BotConfig antes de scan/refresh.
            # Si está apagado, saltamos scan_markets() y om.refresh() para evitar
            # ruido order_version_mismatch. Solo dejamos cancel_stale_orders.
            mm_enabled = True
            if fetch_bot_config is not None:
                try:
                    _cfg = fetch_bot_config() or {}
                    mm_enabled = bool(_cfg.get("strategy_market_maker", False))
                except Exception as _exc:
                    logger.debug("fetch_bot_config fallo (%s); asumo MM apagado", _exc)
                    mm_enabled = False
            else:
                mm_enabled = False

            if not mm_enabled:
                om.cancel_stale_orders()
            else:
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
