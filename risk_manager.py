import json
import os
import logging
from config import CAPITAL_USDC, MAX_DRAWDOWN_PCT, STOP_LOSS_PER_POS, MAX_EXPOSURE_USDC

logger = logging.getLogger(__name__)
STATE_FILE = "state.json"
SHUTDOWN_FLAG = "shutdown.flag"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "capital": CAPITAL_USDC,
        "hwm": CAPITAL_USDC,  # high water mark
        "daily_pnl": 0.0,
        "total_pnl": 0.0,
        "open_positions": {}
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def check_global_stop(state):
    """Comprueba si hay que parar el bot."""
    if os.path.exists(SHUTDOWN_FLAG):
        logger.critical("⛔ shutdown.flag detectado — parando bot")
        return True

    drawdown = (state["hwm"] - state["capital"]) / state["hwm"]
    if drawdown >= MAX_DRAWDOWN_PCT:
        logger.critical(f"⛔ DRAWDOWN GLOBAL {drawdown:.1%} >= {MAX_DRAWDOWN_PCT:.1%} — STOP")
        with open(SHUTDOWN_FLAG, "w") as f:
            f.write(f"Drawdown: {drawdown:.1%}")
        return True

    return False

def check_position_stop(position_id, pnl_usdc, state):
    """Comprueba stop loss por posición."""
    if pnl_usdc <= -STOP_LOSS_PER_POS:
        logger.warning(f"⚠️ Stop loss posición {position_id}: ${pnl_usdc:.2f}")
        return True
    return False

def get_exposure(state):
    """Retorna exposición total actual."""
    return sum(p.get("size", 0) for p in state["open_positions"].values())

def can_open_position(size_usdc, state):
    """Verifica si se puede abrir una nueva posición."""
    exposure = get_exposure(state)
    if exposure + size_usdc > MAX_EXPOSURE_USDC:
        logger.warning(f"Exposición máxima alcanzada: ${exposure:.2f}/${MAX_EXPOSURE_USDC}")
        return False
    if len(state["open_positions"]) >= 3:
        logger.warning("Máximo de 3 posiciones simultáneas alcanzado")
        return False
    return True

def update_capital(pnl_delta, state):
    """Actualiza capital y HWM."""
    state["capital"] += pnl_delta
    state["daily_pnl"] += pnl_delta
    state["total_pnl"] += pnl_delta
    if state["capital"] > state["hwm"]:
        state["hwm"] = state["capital"]
    save_state(state)
    return state
