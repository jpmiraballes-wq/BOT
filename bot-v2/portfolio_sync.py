"""portfolio_sync.py - Sincroniza precios y PnL de Position abiertas.

Cada N iteraciones del main loop se llama PortfolioSync.sync() que:
  - Lee Position con status='open' desde Base44.
  - Para cada una con token_id, pide el midpoint actual al CLOB.
  - Actualiza current_price y pnl_unrealized mediante PUT en la entidad.

pnl_unrealized se calcula como:
    BUY : (current - entry) * size_tokens
    SELL: (entry - current) * size_tokens

Si size_tokens no esta, se aproxima desde size_usdc / entry_price.
"""

import logging

from base44_client import list_records, update_record

logger = logging.getLogger(__name__)


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _compute_pnl(side, entry, current, size_tokens):
    if (side or "BUY").upper() == "SELL":
        return (entry - current) * size_tokens
    return (current - entry) * size_tokens


class PortfolioSync:
    """Actualiza current_price y pnl_unrealized de las posiciones abiertas."""

    def __init__(self, clob_client):
        self.clob = clob_client

    def _get_midpoint(self, token_id):
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
            logger.debug("get_midpoint(%s) fallo: %s",
                         str(token_id)[:10], exc)
            return None

    def sync(self):
        """Actualiza precio/PnL de las positions abiertas. Devuelve #tocadas."""
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

            current = self._get_midpoint(token_id)
            if current is None:
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0

            pnl = _compute_pnl(pos.get("side"), entry, current, size_tokens)

            prev = _safe_float(pos.get("current_price"))
            if prev is not None and abs(prev - current) < 1e-4:
                continue  # sin cambio apreciable, evita spam de writes

            ok = update_record(
                "Position", pos_id,
                {"current_price": round(current, 4),
                 "pnl_unrealized": round(pnl, 4)},
            )
            if ok:
                updated += 1
                logger.info("Position %s: %.3f -> %.3f, pnl=%.2f",
                            str(pos_id)[:8], prev or 0.0, current, pnl)

        if updated:
            logger.info("PortfolioSync: %d posiciones actualizadas.", updated)
        return updated
