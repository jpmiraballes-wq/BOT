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
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb").strip()
POLYMARKET_PROXY_ADDRESS = os.getenv("POLYMARKET_PROXY_ADDRESS", WALLET_ADDRESS).strip()
# 0=EOA, 1=Polymarket email proxy, 2=Gnosis Safe (MetaMask login).
POLYMARKET_SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "2"))

# ---------------------------------------------------------------------------
# Paper trading (dry-run). Corre en paralelo al bot real sin tocar la Safe.
# ---------------------------------------------------------------------------
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
PAPER_CAPITAL_USDC = float(os.getenv("PAPER_CAPITAL_USDC", "2000"))
PAPER_DURATION_DAYS = float(os.getenv("PAPER_DURATION_DAYS", "7"))

# ---------------------------------------------------------------------------
# Base44 (reporting API)
# APP_ID hardcodeado para evitar desincronizacion entre VPS.
# ---------------------------------------------------------------------------
BASE44_APP_ID = "69e1e225a40599eb44ced81e"
BASE44_BASE_URL = "https://trading-bot-app-44ced81e.base44.app"
BASE44_ENTITY = "SystemState"
REPORT_INTERVAL_SECONDS = 5 * 60

# ---------------------------------------------------------------------------
# Capital y riesgo
# ---------------------------------------------------------------------------
# NO_FAKE_CAPITAL_V1 — NO hardcodear capital. El valor real viene de BotConfig.capital_usdc
# leido en main.py al arrancar. Si falla esa lectura, el bot ABORTA arrancada.
# Este 0.0 es solo un sentinel "no configurado", no un fallback operativo.
CAPITAL_USDC = 0.0
MAX_POSITION_PCT = 0.02
MIN_SPREAD_PCT = 0.04
MAX_DRAWDOWN_PCT = 0.15
RESERVE_PCT = 0.20
MAX_TOTAL_EXPOSURE_USDC = 150.0
MAX_LOSS_PER_POSITION_USDC = 1.5
MAX_CONCURRENT_MARKETS = 2
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
BUY_ONLY_MODE = False  # market-making real (BUY+SELL)
PROFIT_TARGET_PCT = 0.15
STOP_LOSS_PCT = -0.30

# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------
GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob-v2.polymarket.com"
POLYGON_CHAIN_ID = 137

# ---------------------------------------------------------------------------
# Loop y sistema
# ---------------------------------------------------------------------------
MAIN_LOOP_INTERVAL_SECONDS = 5  # OVERTAKE_V1_LOOP â Bolt+Opus 2026-04-27 (era 60s)
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
