
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
