
# ---------------------------------------------------------------------------
# Endpoints externos (v2)
# ---------------------------------------------------------------------------
GAMMA_API_URL = "https://gamma-api.polymarket.com"
BASE44_BASE_URL = "https://app.base44.com"
CLOB_API_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137

# ---------------------------------------------------------------------------
# Limites de ejecucion (v2)
# ---------------------------------------------------------------------------
MAX_CONCURRENT_MARKETS = 5
ORDER_MAX_AGE_SECONDS = 2 * 60 * 60
MIN_SPREAD_PCT = 0.02
MAIN_LOOP_INTERVAL_SECONDS = 60

# ---------------------------------------------------------------------------
# Paths (v2)
# ---------------------------------------------------------------------------
from pathlib import Path as _Path
LOG_PATH = _Path(__file__).resolve().parent / "bot.log"
SHUTDOWN_FLAG_PATH = _Path(__file__).resolve().parent / "shutdown.flag"

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

# ---------------------------------------------------------------------------
# Modo solo-BUY (v2)
# Si True, el order_manager NO intenta vender el lado contrario.
# Util cuando la wallet tiene USDC pero aun no posee tokens YES/NO.
# ---------------------------------------------------------------------------
BUY_ONLY_MODE = True
