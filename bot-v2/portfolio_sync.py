"""portfolio_sync.py - Sincroniza precios, PnL y estado de fill de Position.

En cada ciclo:
  * Para cada Position(status='open'):
      - Si pending_fill=True y tiene order_id, consulta el estado de la orden
        en el CLOB autenticado (clob_client.get_order(order_id)):
            size_matched > 0  -> pending_fill=False (confirmada)
            estado CANCELED   -> Position.status='closed',
                                 close_reason='cancelled_no_fill'
            sigue LIVE sin fill -> no toca (sigue esperando)
      - Si pending_fill=False, actualiza current_price y pnl_unrealized con
        el midpoint publico del CLOB.

Funciona tanto en DRY_RUN (paper) como en live; si el clob_client no expone
get_order, simplemente actualiza precios como antes.
"""

import logging
from datetime import datetime, timezone

import requests

from base44_client import list_records, update_record, create_record
from decision_logger import now_iso

logger = logging.getLogger(__name__)

CLOB_PRICE_URL = "https://clob-v2.polymarket.com/price"
REQUEST_TIMEOUT = 8


def _safe_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_side(token_id, side):
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
    """Actualiza precios y estado de fill de las Position abiertas."""

    def __init__(self, clob_client=None):
        self._client = clob_client

    def _order_status(self, order_id):
        """Devuelve dict con (matched, status) o None si no se puede consultar."""
        if not order_id or self._client is None:
            return None
        fn = getattr(self._client, "get_order", None)
        if not callable(fn):
            return None
        try:
            o = fn(order_id) or {}
        except Exception as exc:
            logger.debug("get_order(%s) fallo: %s", str(order_id)[:10], exc)
            return None
        matched = _safe_float(o.get("size_matched") or o.get("sizeMatched")) or 0.0
        status = (o.get("status") or o.get("state") or "").upper()
        return {"matched": matched, "status": status}

    def _confirm_or_cancel_pending(self, pos):
        """Verifica una Position pending_fill. Devuelve accion aplicada."""
        # PENDING_FILL_TIMEOUT_V1
        pos_id = pos.get("id")
        order_id = pos.get("order_id")
        info = self._order_status(order_id)
        if info is None:
            # Fallback: si llevamos > 600s pending y no podemos
            # consultar el CLOB, asumimos que la orden ya esta colocada y
            # liberamos el flag para que auto_close pueda operar.
            opened = pos.get("opened_at") or pos.get("created_date")
            age_ok = False
            if opened:
                try:
                    ts = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    age_ok = age > 600
                except (ValueError, TypeError):
                    age_ok = False
            if age_ok:
                update_record("Position", pos_id, {"pending_fill": False})
                logger.info("pending_fill timeout liberado: pos=%s order=%s",
                            str(pos_id)[:8], str(order_id)[:10])
                return "timeout_cleared"
            return "unknown"

        if info["matched"] > 0:
            update_record("Position", pos_id, {"pending_fill": False})
            logger.info("Fill confirmado: pos=%s order=%s matched=%.2f",
                        str(pos_id)[:8], str(order_id)[:10], info["matched"])
            return "confirmed"

        if info["status"] in ("CANCELED", "CANCELLED", "EXPIRED"):
            # TRADE_ON_CANCEL_V1
            update_record("Position", pos_id, {
                "status": "closed",
                "pending_fill": False,
                "pnl_unrealized": 0.0,
                "pnl_realized": 0.0,
                "close_time": now_iso(),
                "close_reason": "cancelled_no_fill",
            })
            try:
                create_record("Trade", {
                    "market": pos.get("market"),
                    "side": pos.get("side"),
                    "entry_price": pos.get("entry_price"),
                    "exit_price": pos.get("entry_price"),
                    "size_usdc": pos.get("size_usdc") or 0.0,
                    "pnl": 0.0,
                    "pnl_pct": 0.0,
                    "strategy": pos.get("strategy") or "market_maker",
                    "status": "cancelled",
                    "entry_time": pos.get("opened_at"),
                    "exit_time": now_iso(),
                    "notes": f"cancelled_no_fill order={str(order_id)[:10]}",
                })
            except Exception as exc:
                logger.debug("create Trade(cancelled) fallo pos=%s: %s",
                             str(pos_id)[:8], exc)
            logger.info("Orden cancelada sin fill: pos=%s order=%s",
                        str(pos_id)[:8], str(order_id)[:10])
            return "cancelled"

        return "still_pending"

    def sync(self):
        positions = list_records("Position", sort="-updated_date", limit=200) or []
        updated = 0
        pending_skipped = 0
        confirmed = 0
        cancelled = 0

        for pos in positions:
            if pos.get("status") != "open":
                continue
            pos_id = pos.get("id")
            token_id = pos.get("token_id")
            if not pos_id or not token_id:
                continue

            if pos.get("pending_fill"):
                action = self._confirm_or_cancel_pending(pos)
                if action == "confirmed":
                    confirmed += 1
                elif action == "cancelled":
                    cancelled += 1
                else:
                    pending_skipped += 1
                continue

            entry = _safe_float(pos.get("entry_price"))
            if entry is None or entry <= 0:
                continue

            current = _fetch_midpoint(token_id)
            if current is None or current <= 0:
                continue

            size_tokens = _safe_float(pos.get("size_tokens"))
            if size_tokens is None or size_tokens <= 0:
                size_usdc = _safe_float(pos.get("size_usdc")) or 0.0
                size_tokens = size_usdc / entry if entry > 0 else 0.0

            pnl = _compute_pnl(pos.get("side"), entry, current, size_tokens)
            prev = _safe_float(pos.get("current_price"))
            if prev is not None and abs(prev - current) < 1e-4:
                continue

            ok = update_record("Position", pos_id, {
                "current_price": round(current, 4),
                "pnl_unrealized": round(pnl, 4),
            })
            if ok:
                updated += 1

        logger.info(
            "PortfolioSync: updated=%d confirmed=%d cancelled=%d pending=%d",
            updated, confirmed, cancelled, pending_skipped,
        )
