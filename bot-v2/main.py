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
from llm_filter import filter_opportunities

AUTO_CLOSE_EVERY_N_ITERATIONS = 2

PORTFOLIO_SYNC_EVERY_N_ITERATIONS = 2

# News trading corre solo cada N iteraciones para reducir coste LLM.
NEWS_TRADING_EVERY_N_ITERATIONS = 3

# Drawdown diario maximo antes de auto-pausar (fraccion del capital).
DAILY_DRAWDOWN_AUTOPAUSE_PCT = -0.10


MM_STRATEGY = "market_maker"
ARB_STRATEGY = "logical_arb"


def _refresh_kelly_caps(kelly_sizer):
    """Lee StrategySizing desde Base44 y actualiza los caps del Kelly.

    Silencioso ante errores: si falla, se mantiene el cap anterior
    (o el default MAX_POSITION_PCT de config).
    """
    try:
        import os
        import requests
        api_key = os.environ.get("EXTERNAL_BASE44_API_KEY") or os.environ.get("BASE44_API_KEY")
        app_id = os.environ.get("EXTERNAL_BASE44_APP_ID", "69e1e225a40599eb44ced81e")
        base = os.environ.get("BASE44_BASE_URL", "https://app.base44.com")
        if not api_key:
            return
        url = "%s/api/apps/%s/entities/StrategySizing?limit=50" % (base, app_id)
        r = requests.get(url, headers={"api_key": api_key}, timeout=10)
        if r.status_code >= 400:
            return
        records = r.json() if isinstance(r.json(), list) else (r.json().get("records") or r.json().get("data") or [])
        caps = {}
        for rec in records:
            data = rec.get("data") if isinstance(rec.get("data"), dict) else rec
            strat = data.get("strategy")
            pct = data.get("position_size_pct")
            if strat and pct is not None:
                caps[strat] = float(pct)
        if caps:
            kelly_sizer.set_strategy_caps(caps)
    except Exception as exc:
        logging.getLogger(__name__).debug("refresh_kelly_caps fallo: %s", exc)




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


_OPEN_POS_CACHE = {"ts": 0.0, "data": None}
_OPEN_POS_TTL = 30.0


def _open_positions_from_base44():
    """Devuelve (count, sum_size_usdc) leyendo Position de Base44.

    Cuenta solo status=open y pending_fill!=true. Cache de 30s.
    Fallback a (0, 0.0) si falla Base44.
    """
    import time as _time
    now = _time.time()
    if _OPEN_POS_CACHE["data"] is not None and (now - _OPEN_POS_CACHE["ts"]) < _OPEN_POS_TTL:
        return _OPEN_POS_CACHE["data"]
    try:
        from base44_client import list_records
        rows = list_records("Position", sort="-updated_date", limit=100) or []
    except Exception as exc:
        logger.warning("open_positions_from_base44 fallo: %s", exc)
        return (0, 0.0)

    count = 0
    deployed = 0.0
    for r in rows:
        # Base44 externo envuelve en rec["data"]; fallback a raiz.
        d = r.get("data") if isinstance(r, dict) else None
        if not isinstance(d, dict):
            d = r if isinstance(r, dict) else {}
        if d.get("status") != "open":
            continue
        if d.get("pending_fill"):
            continue
        count += 1
        try:
            deployed += float(d.get("size_usdc") or 0.0)
        except (TypeError, ValueError):
            pass
    data = (count, round(deployed, 4))
    _OPEN_POS_CACHE["ts"] = now
    _OPEN_POS_CACHE["data"] = data
    return data


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

    def _field(rec, key):
        # Base44 externo envuelve campos en rec["data"]; fallback a raiz.
        if isinstance(rec, dict):
            d = rec.get("data")
            if isinstance(d, dict) and key in d:
                return d.get(key)
            return rec.get(key)
        return None

    today = _dt.now(_tz.utc).date()
    total_pnl = 0.0
    daily_pnl = 0.0
    wins = 0
    total_closed = 0

    for t in trades:
        # REALIZED_PNL_ONLY_V1: solo contar trades con venta REAL confirmada.
        # Se excluyen:
        #   - trades sin pnl
        #   - trades con status 'cancelled' o 'open'
        #   - trades sin exit_time distinto de entry_time (no hubo salida)
        status = _field(t, "status")
        if status not in ("closed", "filled"):
            continue
        raw_pnl = _field(t, "pnl")
        if raw_pnl is None:
            continue
        try:
            pnl = float(raw_pnl)
        except (TypeError, ValueError):
            continue
        entry_time = _field(t, "entry_time")
        exit_time = _field(t, "exit_time")
        if not exit_time or exit_time == entry_time:
            # Sin cierre real -> no cuenta para stats.
            continue
        entry_price = _field(t, "entry_price")
        exit_price = _field(t, "exit_price")
        if entry_price is not None and exit_price is not None:
            try:
                if float(entry_price) == float(exit_price) and abs(pnl) < 1e-6:
                    # Backfill sintetico (entry=exit, pnl=0) -> no cuenta.
                    continue
            except (TypeError, ValueError):
                pass
        total_pnl += pnl
        total_closed += 1
        if pnl > 0:
            wins += 1
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


def build_snapshot(mode, rm, om, notes="", live_capital=None):
    # capital_deployed y open_positions vienen de la tabla Position
    # (fuente de verdad, no de las open orders del CLOB que desaparecen
    # cuando filean).
    open_count, deployed = _open_positions_from_base44()
    stats = _trade_stats()
    # Prioridad: capital live del BotConfig (fuente de verdad del usuario)
    # > equity del risk manager > CAPITAL_USDC hardcoded.
    if live_capital and float(live_capital) > 0:
        cap_total = float(live_capital)
    else:
        cap_total = rm.current_equity or CAPITAL_USDC
    return {
        "mode": mode,
        "capital_total": cap_total,
        "capital_deployed": deployed,
        "daily_pnl": stats["daily_pnl"],
        "total_pnl": stats["total_pnl"],
        "win_rate": stats["win_rate"],
        "total_trades": stats["total_trades"],
        "drawdown_pct": rm.drawdown_pct,
        "open_positions": open_count,
        "notes": notes,
    }


def build_size_fn(sizer, rm, cb, deployed_fn, budget_fn):
    """FLAT sizing (v2 flat-sizing-fix-2026-04-21).

    Kelly con <30 trades historicos da sizes absurdos (ej $0.75) que
    hacen que con $30 de budget apenas despleguemos $6. Volvemos a
    sizing plano: 15%% del strategy_budget disponible por trade, con
    floor $5 y cap $20. Kelly vuelve cuando tengamos muestra suficiente.
    """
    # FLAT_MAX_75_V1 (2026-04-22): subido de $20 a $75 para tickets
    # reales ~15% del budget MM. Capital ~$500, MM allocated ~$450,
    # 15% = $67.5 -> cap en $75 con margen.
    # FLAT_PCT_25_V1 (2026-04-22): subido de 0.15 a 0.25 para tickets mas
    # grandes. El risk_manager.deployable_capital ya protege del over-deploy.
    FLAT_PCT = 0.25
    FLAT_MIN = 5.0
    FLAT_MAX = 75.0

    def _size(opp):
        mid = float(opp.get("mid") or 0.0)
        edge = float(opp.get("spread_pct") or 0.0) / 2.0
        sizer.record_tick(opp["market_id"], mid)  # mantenemos record para cuando volvamos a Kelly

        if edge <= 0:
            return 0.0

        # Skip trades con edge minusculo. Con fees + slippage + 1 tick
        # en contra perdemos siempre. Minimo 1.5% (configurable via BotConfig).
        try:
            _cfg_min = fetch_bot_config() or {}
            _min_edge = float(_cfg_min.get("min_spread_pct") or 0.03) / 2.0
        except Exception:
            _min_edge = 0.015
        if edge < _min_edge:
            return 0.0

        rm_capital = rm.deployable_capital(deployed_fn())
        strategy_budget = budget_fn()
        capital_available = min(rm_capital, strategy_budget)
        if capital_available <= FLAT_MIN:
            return 0.0

        # Flat: 15%% del capital disponible, con floor y cap.
        raw = capital_available * FLAT_PCT
        size = max(FLAT_MIN, min(FLAT_MAX, raw))
        # Nunca superar el capital disponible.
        size = min(size, capital_available)

        factor = cb.get_size_factor(mid)
        final = size * factor
        if factor < 1.0:
            log_warning(
                "size_reducido_precio_extremo",
                module="market_maker",
                extra={"market": opp.get("question"), "mid": mid,
                       "factor": factor, "size": final},
            )
        return final
    return _size


_DD_AUTOPAUSE_TRIGGERED_DATE = {"date": None}


def _check_daily_drawdown_autopause():
    """Si daily_pnl cae por debajo de DAILY_DRAWDOWN_AUTOPAUSE_PCT del capital,
    marca BotConfig.paused=true y manda alerta Telegram.

    Solo se dispara UNA vez por dia (clave: fecha UTC).
    El usuario debe despausar manualmente desde el dashboard.
    """
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.now(_tz.utc).date().isoformat()
    if _DD_AUTOPAUSE_TRIGGERED_DATE["date"] == today:
        return  # ya alertamos hoy, no repetir

    try:
        stats = _trade_stats()
        daily_pnl = float(stats.get("daily_pnl") or 0.0)
        # Lee capital REAL desde BotConfig (fuente de verdad del usuario).
        # Antes usaba CAPITAL_USDC (constante vieja) lo que disparaba auto-pause
        # con perdidas minusculas al calcular sobre capital desfasado.
        capital = float(CAPITAL_USDC)
        try:
            _cfg = fetch_bot_config() or {}
            _live_cap = _cfg.get("capital_usdc")
            if _live_cap is not None and float(_live_cap) > 0:
                capital = float(_live_cap)
        except Exception:
            pass
        if capital <= 0:
            return
        dd_frac = daily_pnl / capital
        if dd_frac > DAILY_DRAWDOWN_AUTOPAUSE_PCT:
            return  # sin drawdown critico

        # Gatillo: pausar bot via BotConfig
        try:
            from base44_client import list_records, update_record
            cfgs = list_records("BotConfig", limit=1) or []
            if cfgs:
                cfg_id = cfgs[0].get("id")
                if cfg_id:
                    update_record("BotConfig", cfg_id, {
                        "paused": True,
                        "notes": "auto_pause: daily_pnl=%.2f USDC (%.2f%% del capital)" % (
                            daily_pnl, dd_frac * 100.0,
                        ),
                    })
        except Exception as _exc:
            logger.error("auto_pause write BotConfig fallo: %s", _exc)

        # Alertar Telegram
        try:
            import os
            import requests
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            chat = os.environ.get("TELEGRAM_CHAT_ID")
            if token and chat:
                msg = ("\ud83d\udea8 *AUTO-PAUSE por drawdown diario*\n\n"
                       "daily_pnl: *$%.2f* (%.2f%% del capital)\n"
                       "umbral: %.1f%%\n\n"
                       "Bot pausado automaticamente. Revisa y despausa desde el dashboard.") % (
                    daily_pnl, dd_frac * 100.0,
                    DAILY_DRAWDOWN_AUTOPAUSE_PCT * 100.0,
                )
                requests.post(
                    "https://api.telegram.org/bot%s/sendMessage" % token,
                    json={"chat_id": chat, "text": msg, "parse_mode": "Markdown"},
                    timeout=10,
                )
        except Exception as _exc:
            logger.error("auto_pause telegram fallo: %s", _exc)

        logger.critical(
            "AUTO-PAUSE activado: daily_pnl=%.2f USDC (%.2f%% del capital, umbral %.1f%%)",
            daily_pnl, dd_frac * 100.0, DAILY_DRAWDOWN_AUTOPAUSE_PCT * 100.0,
        )
        _DD_AUTOPAUSE_TRIGGERED_DATE["date"] = today
    except Exception as _exc:
        logger.debug("check_daily_drawdown_autopause fallo: %s", _exc)




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

    # _SHUTDOWN_FLAG_CLEAR_ON_START: limpiar flag huerfano de runs anteriores.
    # Si el flag quedo en disco tras un SIGTERM previo, crash o kill -9,
    # el loop principal veria exists()=True en la primera iteracion y haria
    # "clean shutdown" inmediato -> bucle con systemd Restart=always.
    try:
        if SHUTDOWN_FLAG_PATH.exists():
            SHUTDOWN_FLAG_PATH.unlink()
            logger.warning("shutdown.flag huerfano encontrado al arrancar y borrado")
    except OSError as _flag_exc:
        logger.error("no pude borrar shutdown.flag al arrancar: %s", _flag_exc)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rm = RiskManager()
    reporter = Reporter()
    # PAPER TRADING DESACTIVADO PERMANENTEMENTE
    # (antes: if DRY_RUN -> PaperBroker. Eliminado por el usuario el 2026-04-20
    # porque generaba trades falsos que contaminaban el dashboard.)
    logger.info("=== MODO LIVE TRADING FORZADO (paper trading desactivado) ===")
    om = OrderManager()
    paper_reporter = None
    sizer = KellySizer()
    _refresh_kelly_caps(sizer)  # carga inicial desde Base44
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

    # Sync capital real desde BotConfig antes del primer heartbeat.
    try:
        _initial_cfg = fetch_bot_config() or {}
        rm.update_capital(_initial_cfg.get("capital_usdc"))
    except Exception as _sync_exc:
        logger.warning("Sync inicial de capital fallo: %s", _sync_exc)
        _initial_cfg = {}
    reporter.report(build_snapshot("running", rm, om,
                                   live_capital=_initial_cfg.get("capital_usdc")),
                    force=True)

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
        # Refrescar caps Kelly cada 60 iteraciones desde Base44
        if iteration % 60 == 0:
            _refresh_kelly_caps(sizer)
        # Chequear drawdown diario y auto-pausar si corresponde
        _check_daily_drawdown_autopause()
        loop_started = time.time()
        # ---- Control remoto desde dashboard (BotConfig) ----
        try:
            bot_cfg = fetch_bot_config() or {}
        except Exception as _cfg_exc:
            logger.warning("fetch_bot_config fallo: %s", _cfg_exc)
            bot_cfg = {}
        # Sync equity con capital real del BotConfig cada ciclo.
        try:
            rm.update_capital(bot_cfg.get("capital_usdc"))
        except Exception as _sync_exc:
            logger.warning("rm.update_capital fallo: %s", _sync_exc)
        # WIRE_DYNAMIC_PCT_V1: sincronizamos tambien max_position_pct
        # desde BotConfig. Si el setter existe (patch DYNAMIC_MAX_POSITION_PCT_V1)
        # se usa; sino es noop.
        try:
            _setter = getattr(rm, "set_dynamic_max_position_pct", None)
            if callable(_setter):
                _setter(bot_cfg.get("max_position_pct"))
        except Exception as _pct_exc:
            logger.warning("rm.set_dynamic_max_position_pct fallo: %s", _pct_exc)
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
            reporter.report(build_snapshot("stopped", rm, om, notes="emergency_stop_dashboard",
                                           live_capital=bot_cfg.get("capital_usdc")),
                            force=True)
            break
        if bot_cfg.get("paused"):
            logger.info("Bot pausado desde dashboard. Solo mantenimiento.")
            try:
                om.cancel_stale_orders()
            except Exception as _e:
                logger.error("cancel_stale_orders en pausa fallo: %s", _e)
            reporter.report(build_snapshot("paused", rm, om, notes="paused_dashboard",
                                           live_capital=bot_cfg.get("capital_usdc")))
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
                opportunities = filter_opportunities(opportunities, MM_STRATEGY)
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
            # STABILITY_PATCH_B_V1: solo corren estrategias is_enabled segun dashboard.
            if (news_trader is not None
                    and allocator.is_enabled("news_trading")
                    and iteration % NEWS_TRADING_EVERY_N_ITERATIONS == 0):
                try:
                    news_trader.run_cycle()
                except Exception as _exc:
                    logger.error("news_trading fallo: %s", _exc)

            if stat_arb is not None and allocator.is_enabled("stat_arb"):
                try:
                    stat_arb.run_cycle()
                except Exception as _exc:
                    logger.error("stat_arb fallo: %s", _exc)

            if res_sniper is not None and allocator.is_enabled("resolution_snipe"):
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
                        arb_opps_filtered = filter_opportunities(arb_opps, ARB_STRATEGY)
                        results = run_umbrella_cycle(om, arb_opps_filtered, max_per_cycle=2)
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
            # Aislado en try/except: si build_snapshot() falla (CLOB caido,
            # rm corrupto, etc.) mandamos heartbeat minimo para no quedar
            # "clavado" en el dashboard.
            try:
                reporter.report(build_snapshot("running", rm, om, live_capital=bot_cfg.get("capital_usdc")))
            except Exception as _hb_exc:
                logger.error("build_snapshot fallo, mando heartbeat minimo: %s",
                             _hb_exc)
                try:
                    reporter.send_minimal_heartbeat("running",
                                                   notes=str(_hb_exc)[:100])
                except Exception as _min_exc:
                    logger.error("minimal_heartbeat tambien fallo: %s", _min_exc)

            # AUTOCLOSE_RESPECT_PAUSED_V1: si el bot esta pausado desde
            # el dashboard, NO corremos AutoClose. Antes corria igual y
            # seguia cerrando dust + creando Trades fantasma (pnl=0)
            # incluso con paused=true. Esto creaba cientos de trades
            # basura por noche.
            _paused_now = bool(bot_cfg.get("paused"))
            if aclose is not None and not _paused_now and iteration % AUTO_CLOSE_EVERY_N_ITERATIONS == 0:
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
