"""paper_lab.py - Emite PaperTrade a Base44 para estrategias en modo 'paper'.

Se invoca desde cada estrategia justo en el punto donde, en modo live,
haria self.om.place_limit_buy(...). En modo paper:
  - NO toca el CLOB
  - NO consume capital real (deployed no cambia)
  - Solo registra el PaperTrade con entry_price, features, tp/sl/timeout

El auto-close corre desde Base44 (funcion paperTradeClose, scheduled cada 5min).

# PAPER_LAB_PATCHED
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _endpoint() -> str:
    return "%s/api/apps/%s/entities/PaperTrade" % (
        BASE44_BASE_URL, BASE44_APP_ID,
    )


def emit_paper_trade(
    *,
    strategy: str,
    market: str,
    side: str,
    entry_price: float,
    size_usdc: float,
    token_id: Optional[str] = None,
    condition_id: Optional[str] = None,
    outcome: Optional[str] = None,
    tp_pct: Optional[float] = None,
    sl_pct: Optional[float] = None,
    max_hold_hours: Optional[float] = None,
    features: Optional[Dict[str, Any]] = None,
    signal_meta: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Optional[str]:
    """Crea un PaperTrade en Base44. Devuelve el id o None si falla.

    - entry_price: precio mid al momento de la senal (0-1)
    - simulated_entry_price: se calcula agregando un slippage pequeno
    - tp_pct/sl_pct: fracciones (ej 0.05, -0.06). Usados por paperTradeClose.
    - max_hold_hours: timeout. Default 24h si no se pasa.
    """
    if not BASE44_API_KEY:
        logger.warning("paper_lab: BASE44_API_KEY vacia, no emito paper trade")
        return None

    # Slippage simulado: 0.5% peor para BUY (pagas el ask), 0.5% mejor para SELL.
    if (side or "").upper() == "BUY":
        sim_entry = float(entry_price) * 1.005
    else:
        sim_entry = float(entry_price) * 0.995

    payload: Dict[str, Any] = {
        "strategy": strategy,
        "market": market[:500] if market else strategy,
        "side": (side or "BUY").upper(),
        "entry_price": float(entry_price),
        "simulated_entry_price": round(sim_entry, 6),
        "current_price": float(entry_price),
        "size_usdc": float(size_usdc),
        "status": "open",
        "entry_time": datetime.now(timezone.utc).isoformat(),
        "pnl_unrealized": 0.0,
        "simulated_fees_usdc": 0.0,
        "take_profit_pct": float(tp_pct) if tp_pct is not None else 0.05,
        "stop_loss_pct": float(sl_pct) if sl_pct is not None else -0.06,
        "max_hold_hours": float(max_hold_hours) if max_hold_hours is not None else 24.0,
    }
    if token_id:
        payload["token_id"] = token_id
    if condition_id:
        payload["condition_id"] = condition_id
    if outcome:
        payload["outcome"] = outcome
    if features:
        # Solo mantener keys basicas para evitar payloads gigantes
        clean: Dict[str, Any] = {}
        for k, v in features.items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                clean[k] = v
        payload["features_at_entry"] = clean
    if signal_meta:
        clean_meta: Dict[str, Any] = {}
        for k, v in signal_meta.items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                clean_meta[k] = v
        payload["signal_metadata"] = clean_meta
    if notes:
        payload["notes"] = notes[:500]

    try:
        resp = requests.post(
            _endpoint(),
            json=payload,
            headers={
                "api_key": BASE44_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("paper_lab POST %d: %s", resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json() or {}
            pid = data.get("id") or (data.get("data") or {}).get("id")
            logger.info("paper_lab emitted %s %s %s size=$%.2f entry=%.4f id=%s",
                        strategy, side, market[:60] if market else "?",
                        size_usdc, entry_price, pid)
            return pid
        except ValueError:
            return "ok"
    except requests.RequestException as exc:
        logger.error("paper_lab POST error: %s", exc)
        return None
