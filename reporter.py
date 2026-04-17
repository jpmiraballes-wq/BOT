import requests
import logging
import time
from config import BASE44_API_KEY, BASE44_URL

logger = logging.getLogger(__name__)
_last_report = 0
REPORT_INTERVAL = 300  # 5 minutos

def report_system_state(state, mode="paper"):
    """Reporta estado del sistema a Base44."""
    global _last_report
    now = time.time()
    if now - _last_report < REPORT_INTERVAL:
        return

    capital = state.get("capital", 0)
    hwm = state.get("hwm", capital)
    drawdown = ((hwm - capital) / hwm * 100) if hwm > 0 else 0
    open_pos = len(state.get("open_positions", {}))

    payload = {
        "mode": mode,
        "capital_total": round(capital, 2),
        "capital_deployed": round(state.get("deployed", 0), 2),
        "capital_reserved": round(capital * 0.20, 2),
        "daily_pnl": round(state.get("daily_pnl", 0), 2),
        "total_pnl": round(state.get("total_pnl", 0), 2),
        "drawdown_pct": round(drawdown, 2),
        "open_positions": open_pos,
        "last_heartbeat": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    try:
        resp = requests.post(
            f"{BASE44_URL}/SystemState/records",
            json=payload,
            headers={"api_key": BASE44_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
        _last_report = now
        logger.info(f"✅ Estado reportado a Base44 — Capital: ${capital:.2f}")
    except Exception as e:
        logger.error(f"Error reportando a Base44: {e}")

def report_trade(trade_data):
    """Registra un trade en Base44."""
    try:
        resp = requests.post(
            f"{BASE44_URL}/Trade/records",
            json=trade_data,
            headers={"api_key": BASE44_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
        logger.info(f"✅ Trade registrado: {trade_data.get('market', '')}")
    except Exception as e:
        logger.error(f"Error registrando trade: {e}")

def report_signal(signal_data):
    """Registra una señal en Base44."""
    try:
        resp = requests.post(
            f"{BASE44_URL}/Signal/records",
            json=signal_data,
            headers={"api_key": BASE44_API_KEY},
            timeout=10
        )
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Error registrando señal: {e}")

def log_event(level, message, module="main"):
    """Registra un evento en Base44."""
    try:
        requests.post(
            f"{BASE44_URL}/LogEvent/records",
            json={"level": level, "message": message, "module": module},
            headers={"api_key": BASE44_API_KEY},
            timeout=5
        )
    except:
        pass
