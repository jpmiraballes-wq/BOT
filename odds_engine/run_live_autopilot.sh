#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# CCC live autopilot: guarded buy + sell loop.
# Pull latest before running this script:
#   cd /Users/juanmiraballes/BOT && git pull
# Then:
#   cd /Users/juanmiraballes/BOT/odds_engine && bash run_live_autopilot.sh

mkdir -p data logs

export PYTHONUNBUFFERED=1

# Keep disaster stop wide for sports volatility.
export DATA_EXIT_STOP_LIVE_PCT="${DATA_EXIT_STOP_LIVE_PCT:--35}"
export DATA_EXIT_STOP_UNKNOWN_PCT="${DATA_EXIT_STOP_UNKNOWN_PCT:--30}"
export DATA_EXIT_STOP_SEASON_PCT="${DATA_EXIT_STOP_SEASON_PCT:--45}"

# Defensive exposure is separate from disaster stop.
export DATA_EXIT_DEFENSIVE_LOSS_LIVE_PCT="${DATA_EXIT_DEFENSIVE_LOSS_LIVE_PCT:--15}"
export DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_LIVE_USD="${DATA_EXIT_DEFENSIVE_MIN_EXPOSURE_LIVE_USD:-25}"
export DATA_EXIT_DEFENSIVE_COOLDOWN_SECONDS="${DATA_EXIT_DEFENSIVE_COOLDOWN_SECONDS:-900}"

# Buy risk caps. Raise later only after logs are clean.
MAX_ORDER_USD="${MAX_ORDER_USD:-7}"
MAX_CYCLE_NOTIONAL_USD="${MAX_CYCLE_NOTIONAL_USD:-45}"
MAX_SESSION_NOTIONAL_USD="${MAX_SESSION_NOTIONAL_USD:-250}"
MAX_MARKETS="${MAX_MARKETS:-8}"
MAX_POSITIONS="${MAX_POSITIONS:-40}"
MAX_MARKET_EXPOSURE_USD="${MAX_MARKET_EXPOSURE_USD:-18}"
MAX_EVENT_EXPOSURE_USD="${MAX_EVENT_EXPOSURE_USD:-35}"
DAILY_LOSS_LIMIT_USD="${DAILY_LOSS_LIMIT_USD:-35}"
ORDER_TTL="${ORDER_TTL:-45}"
SLEEP_SECONDS="${SLEEP_SECONDS:-45}"
CYCLES="${CYCLES:-999999}"

STAMP="$(date +%Y%m%d_%H%M%S)"
LOG="logs/ccc_live_autopilot_${STAMP}.log"

echo "CCC_LIVE_AUTOPILOT_LAUNCH log=$LOG"
echo "Risk caps: max_order=$MAX_ORDER_USD max_cycle=$MAX_CYCLE_NOTIONAL_USD max_session=$MAX_SESSION_NOTIONAL_USD max_markets=$MAX_MARKETS order_ttl=$ORDER_TTL"

exec .venv/bin/python -u ccc_live_autopilot.py \
  --real \
  --cycles "$CYCLES" \
  --sleep "$SLEEP_SECONDS" \
  --order-ttl "$ORDER_TTL" \
  --max-order-usd "$MAX_ORDER_USD" \
  --max-cycle-notional-usd "$MAX_CYCLE_NOTIONAL_USD" \
  --max-session-notional-usd "$MAX_SESSION_NOTIONAL_USD" \
  --max-markets "$MAX_MARKETS" \
  --max-positions "$MAX_POSITIONS" \
  --max-market-exposure-usd "$MAX_MARKET_EXPOSURE_USD" \
  --max-event-exposure-usd "$MAX_EVENT_EXPOSURE_USD" \
  --daily-loss-limit-usd "$DAILY_LOSS_LIMIT_USD" \
  --kill-old \
  --compile 2>&1 | tee "$LOG"
