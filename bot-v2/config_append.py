# ---------------------------------------------------------------------------
# Endpoints externos (requeridos por los modulos v2)
# ---------------------------------------------------------------------------
# Usado por market_scanner.py y logical_arb.py
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Usado por base44_client.py para reportar LogEvent / Opportunity / etc.
BASE44_BASE_URL = "https://api.base44.com/api"

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
