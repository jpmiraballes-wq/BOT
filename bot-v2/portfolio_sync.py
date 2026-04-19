"""portfolio_sync.py - Sincroniza precios y PnL de Position abiertas.

Usa el endpoint publico del CLOB (sin firma) para obtener el mid de cada
token, y actualiza via PUT cada Position con current_price + pnl_unrealized.

Funciona tanto en DRY_RUN (paper) como en live, porque no depende del
ClobClient autenticado (que en paper es PaperBroker y no expone get_midpoint).
"""

import logging

import requests

from base44_client import list_records, update_record

logger = logging.getLogger(__name__)

CLOB_PRICE_URL = "https://clob.polymarket.com/price"
REQUEST_TIMEOUT = 8


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_side(token_id, side):
    """Pide el mejor precio en un lado. None si falla."""
    try:
        resp = requests.get(
            CLOB_PRICE_URL,
            params={"token_id": token_id, "side": side},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return _safe_float(data.get("price"))
    except (requests.RequestException, ValueError):
        return None


def _fetch_midpoint(token_id):
    if not token_id:
        return None
    buy = _fetch_side(token_id, "BUY")
    sell = _fetch_side(token_id, "SELL")
    if buy is not None and sell is not None and buy > 0 and sell > 0:
        return (buy + sell) / 2.0
    return buy if buy else sell


def _compute_pnl(side, entry, current, size_tokens):
    if (side or "BUY").upper() == "SELL":
        return (entry - current) * size_tokens
    return (current - entry) * size_tokens


class PortfolioSync:
    """Actualiza current_price y pnl_unrealized de las posiciones abiertas."""

    def __init__(self, clob_client=None):
        # clob_client se acepta por compatibilidad pero no se usa.
        self.clob = clob_client

    def sync(self):
        positions = list_records("Position", sort="-updated_date", limit=200) or []
        updated = 0
        skipped_no_token = 0
        skipped_no_price = 0
        skipped_no_change = 0
        checked = 0

        for pos in positions:
            if pos.get("status") != "open":
                continue
            pos_id = pos.get("id")
            token_id = pos.get("token_id")
            if not pos_id or not token_id:
                skipped_no_token += 1
                continue

            entry = _safe_float(pos.get("entry_price"))
            if entry is None or entry <= 0:
                continue

            checked += 1
            current = _fetch_midpoint(token_id)
            if current is None or current <= 0:
                skipped_no_price += 1
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0

            pnl = _compute_pnl(pos.get("side"), entry, current, size_tokens)

            prev = _safe_float(pos.get("current_price"))
            if prev is not None and abs(prev - current) < 1e-4:
                skipped_no_change += 1
                continue

            ok = update_record(
                "Position", pos_id,
                {"current_price": round(current, 4),
                 "pnl_unrealized": round(pnl, 4)},
            )
            if ok:
                updated += 1
                logger.info(
                    "Position %s (%s): %.4f -> %.4f pnl=%+.2f",
                    str(pos_id)[:8], (pos.get("side") or "")[:3],
                    prev or 0.0, current, pnl,
                )

        logger.info(
            "PortfolioSync: checked=%d updated=%d no_token=%d no_price=%d no_change=%d",
            checked, updated, skipped_no_token, skipped_no_price, skipped_no_change,
        )
        return updated
