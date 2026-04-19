"""config.py - Configuracion central del bot Polymarket v2.

Carga variables de entorno desde .env (credenciales) y expone constantes
compartidas por el resto de modulos. Los parametros no secretos viven
aqui para que git pull los mantenga sincronizados entre VPS.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# ---------------------------------------------------------------------------
# Credenciales (desde .env local)
# ---------------------------------------------------------------------------
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
BASE44_API_KEY = os.getenv("BASE44_API_KEY", "").strip()

# Polymarket proxy/funder (para firmar ordenes CLOB). Default: WALLET_ADDRESS.
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", WALLET_ADDRESS).strip()
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", WALLET_ADDRESS).strip()

# ---------------------------------------------------------------------------
# Base44 (reporting API)
# APP_ID hardcodeado para evitar desincronizacion entre VPS.
# ---------------------------------------------------------------------------
BASE44_APP_ID = "69e1e225a40599eb44ced81e"
BASE44_BASE_URL = "https://app.base44.com"
BASE44_ENTITY = "SystemState"
REPORT_INTERVAL_SECONDS = 5 * 60

# ---------------------------------------------------------------------------
# Capital y riesgo
# ---------------------------------------------------------------------------
CAPITAL_USDC = 30.0
MAX_POSITION_PCT = 0.05
MIN_SPREAD_PCT = 0.02
MAX_DRAWDOWN_PCT = 0.15
RESERVE_PCT = 0.20
MAX_TOTAL_EXPOSURE_USDC = 150.0
MAX_LOSS_PER_POSITION_USDC = 5.0
MAX_CONCURRENT_MARKETS = 5
ORDER_MAX_AGE_SECONDS = 2 * 60 * 60

# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------
INTRADAY_DD_PAUSE_PCT = 0.05
INTRADAY_DD_PAUSE_SECONDS = 30 * 60
EXTREME_LOW = 0.08
EXTREME_HIGH = 0.92
EXTREME_SIZE_FACTOR = 1.0 / 3.0
MIN_HOURS_TO_RESOLUTION = 6

# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------
KELLY_FRACTION = 0.25
KELLY_VARIANCE_WINDOW = 60
KELLY_MIN_VARIANCE = 1e-4

# ---------------------------------------------------------------------------
# Logical arb
# ---------------------------------------------------------------------------
LOGICAL_ARB_OVER_THRESHOLD = 1.02
LOGICAL_ARB_UNDER_THRESHOLD = 0.97

# ---------------------------------------------------------------------------
# Modo
# ---------------------------------------------------------------------------
BUY_ONLY_MODE = True
PROFIT_TARGET_PCT = 0.15
STOP_LOSS_PCT = -0.30

# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# ---------------------------------------------------------------------------
# Loop y sistema
# ---------------------------------------------------------------------------
MAIN_LOOP_INTERVAL_SECONDS = 60
SHUTDOWN_FLAG_PATH = BASE_DIR / "shutdown.flag"
STATE_FILE_PATH = BASE_DIR / "state.json"
LOG_PATH = str(BASE_DIR / "bot.log")


def validate_config() -> None:
    missing = []
    if not WALLET_ADDRESS:
        missing.append("WALLET_ADDRESS")
    if not PRIVATE_KEY:
        missing.append("PRIVATE_KEY")
    if not BASE44_API_KEY:
        missing.append("BASE44_API_KEY")
    if missing:
        raise EnvironmentError(
            "Faltan variables de entorno: %s. Revisa %s" %
            (", ".join(missing), ENV_PATH)
        )
