#!/usr/bin/env bash
# run_bot.sh — Wrapper infinito del bot Polymarket (AUTO_UPDATE_V1).
#
# - Activa venv
# - Lanza main.py
# - Si sale con 0 → relanza inmediatamente (update exitoso)
# - Si sale con != 0 → espera 10s y relanza (crash / error)
# - Soporta Ctrl+C para parar de verdad (trap SIGTERM/SIGINT)

set -u

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

# Log rotation simple: si bot.log > 50MB lo movemos
LOG_FILE="$HOME/bot.log"
if [[ -f "$LOG_FILE" ]]; then
  size=$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)
  if (( size > 52428800 )); then
    mv "$LOG_FILE" "$LOG_FILE.old"
  fi
fi

# Trap para salida limpia
SHUTDOWN=0
trap 'SHUTDOWN=1; echo "[run_bot] shutdown requested"; kill $CHILD 2>/dev/null; exit 0' SIGTERM SIGINT

while true; do
  if [[ $SHUTDOWN -eq 1 ]]; then break; fi

  echo "[run_bot $(date '+%Y-%m-%d %H:%M:%S')] starting main.py..."

  # Elegir binario python del venv (launchd no carga shells interactivos,
  # por eso apuntamos directo al binario en vez de 'source activate').
  if [[ -x "$BOT_DIR/.venv/bin/python" ]]; then
    PY_BIN="$BOT_DIR/.venv/bin/python"
  elif [[ -x "$BOT_DIR/venv/bin/python" ]]; then
    PY_BIN="$BOT_DIR/venv/bin/python"
  else
    PY_BIN="python3"
  fi

  "$PY_BIN" main.py >> "$LOG_FILE" 2>&1 &
  CHILD=$!
  wait $CHILD
  EXIT_CODE=$?

  if [[ $SHUTDOWN -eq 1 ]]; then break; fi

  if [[ $EXIT_CODE -eq 0 ]]; then
    echo "[run_bot $(date '+%Y-%m-%d %H:%M:%S')] exit 0 (auto-update). Relanzando..." >> "$LOG_FILE"
    sleep 2
  else
    echo "[run_bot $(date '+%Y-%m-%d %H:%M:%S')] exit $EXIT_CODE (crash). Reintentando en 10s..." >> "$LOG_FILE"
    sleep 10
  fi
done

echo "[run_bot $(date '+%Y-%m-%d %H:%M:%S')] shutdown clean." >> "$LOG_FILE"
