import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Endpoints externos (requeridos por los modulos v2)
# ---------------------------------------------------------------------------
# Usado por market_scanner.py y logical_arb.py
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Usado por base44_client.py para reportar LogEvent / Opportunity / etc.
# (el cliente ya anade "/api/apps/..." por su cuenta, no incluir "/api" aqui)
BASE44_BASE_URL = "https://app.base44.com"

# Polymarket CLOB
CLOB_API_URL = os.environ.get("CLOB_API_URL", "https://clob.polymarket.com")
POLYGON_CHAIN_ID = int(os.environ.get("POLYGON_CHAIN_ID", "137"))

# ---------------------------------------------------------------------------
# Order manager / loop (v2)
# ---------------------------------------------------------------------------
MAX_CONCURRENT_MARKETS = int(os.environ.get("MAX_CONCURRENT_MARKETS", "3"))
ORDER_MAX_AGE_SECONDS = int(os.environ.get("ORDER_MAX_AGE_SECONDS", "300"))
MIN_SPREAD_PCT = float(os.environ.get("MIN_SPREAD_PCT", "0.02"))
MAIN_LOOP_INTERVAL_SECONDS = int(os.environ.get("MAIN_LOOP_INTERVAL_SECONDS", "60"))

# Rutas (se pueden sobreescribir con variables de entorno)
LOG_PATH = Path(os.environ.get("LOG_PATH", str(Path.home() / "Desktop" / "bot" / "bot.log")))
SHUTDOWN_FLAG_PATH = Path(os.environ.get("SHUTDOWN_FLAG_PATH", str(Path.home() / "Desktop" / "bot" / "shutdown.flag")))

# ---------------------------------------------------------------------------
# Circuit breakers (v2)
# ---------------------------------------------------------------------------
INTRADAY_DD_PAUSE_PCT = 0.05
INTRADAY_DD_PAUSE_SECONDS = 30 * 60
EXTREME_LOW = 0.08
EXTREME_HIGH = 0.92
EXTREME_SIZE_FACTOR = 1.0 / 3.0
MIN_HOURS_TO_RESOLUTION = 6

# ---------------------------------------------------------------------------
# Kelly fraccional (v2)
# ---------------------------------------------------------------------------
KELLY_FRACTION = 0.25
KELLY_VARIANCE_WINDOW = 60
KELLY_MIN_VARIANCE = 1e-4

# ---------------------------------------------------------------------------
# Arbitraje logico (v2)
# ---------------------------------------------------------------------------
LOGICAL_ARB_OVER_THRESHOLD = 1.02
LOGICAL_ARB_UNDER_THRESHOLD = 0.97
