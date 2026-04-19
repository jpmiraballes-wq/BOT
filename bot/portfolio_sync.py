"""portfolio_sync.py - Sincroniza precios y PnL de las Position abiertas.

Cada ciclo (o cada N ciclos) recorre las Position con status='open' que tengan
token_id y consulta el midpoint actual del CLOB. Actualiza en Base44:
    - current_price
    - pnl_unrealized

pnl_unrealized = (current_price - entry_price) * size_tokens   # BUY
                 (entry_price - current_price) * size_tokens   # SELL

Si no hay size_tokens, se aproxima desde size_usdc / entry_price.
"""

import logging
from typing import Any, Dict, List, Optional

from base44_client import list_records, update_record

logger = logging.getLogger(__name__)


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _compute_pnl(side: str, entry: float, current: float, size_tokens: float) -> float:
    if side == "SELL":
        return (entry - current) * size_tokens
    return (current - entry) * size_tokens


class PortfolioSync:
    """Actualiza current_price y pnl_unrealized de las posiciones abiertas."""

    def __init__(self, clob_client) -> None:
        self.clob = clob_client

    def _get_midpoint(self, token_id: str) -> Optional[float]:
        if not token_id or self.clob is None:
            return None
        try:
            resp = self.clob.get_midpoint(token_id)
            if isinstance(resp, dict):
                mid = resp.get("mid") or resp.get("midpoint")
            else:
                mid = resp
            return _safe_float(mid)
        except Exception as exc:
            logger.debug("get_midpoint(%s) fallo: %s", token_id[:10], exc)
            return None

    def sync(self) -> int:
        """Lee positions abiertas y actualiza precio/PnL. Devuelve cuantas se tocaron."""
        positions = list_records("Position", sort="-updated_date", limit=200) or []
        updated = 0

        for pos in positions:
            if pos.get("status") != "open":
                continue

            pos_id = pos.get("id")
            token_id = pos.get("token_id")
            if not pos_id or not token_id:
                continue

            entry = _safe_float(pos.get("entry_price"))
            if entry is None or entry <= 0:
                continue

            side = (pos.get("side") or "BUY").upper()
            current = self._get_midpoint(token_id)
            if current is None:
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0

            pnl = _compute_pnl(side, entry, current, size_tokens)

            prev_current = _safe_float(pos.get("current_price"))
            if prev_current is not None and abs(prev_current - current) < 1e-4:
                # Sin cambio real de precio, evitamos spam de writes.
                continue

            ok = update_record(
                "Position",
                pos_id,
                {"current_price": round(current, 4),
                 "pnl_unrealized": round(pnl, 4)},
            )
            if ok:
                updated += 1
                logger.info("Position %s: price %.3f -> %.3f, pnl=%.2f",
                            pos_id[:8], prev_current or 0.0, current, pnl)

        if updated:
            logger.info("PortfolioSync: %d posiciones actualizadas.", updated)
        return updated
