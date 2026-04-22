"""contrarian_fade.py - Fadea movimientos bruscos >X% en <Y min sin noticia confirmable.

PAPER-ONLY POR DISENO. Entry: si un mercado se movio >15% en <60 min y no
detectamos titular relacionado reciente -> asumimos overreaction -> fade.
SELL sintetico = BUY del outcome OPUESTO.

Usa Gamma API publica (misma que stat_arb) + MarketSnapshot para detectar movimientos.
La deteccion sin news es best-effort (no consulta RSS aqui, solo usa ventana temporal).

# PAPER_LAB_PHASE3
"""

import logging
import time
from typing import Any, Dict, List, Optional

import requests

from capital_allocator import CapitalAllocator
from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL
from paper_lab import emit_paper_trade

logger = logging.getLogger(__name__)

STRATEGY = "contrarian_fade"
GAMMA_API_URL = "https://gamma-api.polymarket.com"
REQUEST_TIMEOUT = 15

# Defaults
MIN_MOVE_PCT = 15.0
MOVE_WINDOW_MIN = 60
MIN_VOLUME_USDC = 50_000
POSITION_SIZE_USDC = 5.0
TP_PCT = 0.10
SL_PCT = -0.08
MAX_HOLD_HOURS = 12.0


def _endpoint(entity: str) -> str:
    return "%s/api/apps/%s/entities/%s/records" % (
        BASE44_BASE_URL, BASE44_APP_ID, entity,
    )


def _fetch_active_markets() -> List[Dict[str, Any]]:
    url = "%s/markets" % GAMMA_API_URL
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": 200, "order": "volume", "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        return data if isinstance(data, list) else []
    except requests.RequestException:
        return []


def _extract_yes_price(market: Dict[str, Any]) -> Optional[float]:
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            import json as _j
            prices = _j.loads(prices)
        except Exception:
            return None
    if isinstance(prices, list) and prices:
        try:
            return float(prices[0])
        except (TypeError, ValueError):
            return None
    return None


def _extract_token_ids(market: Dict[str, Any]) -> List[str]:
    tokens = market.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            import json as _j
            tokens = _j.loads(tokens)
        except Exception:
            return []
    return list(tokens) if isinstance(tokens, list) else []


def _fetch_recent_snapshot(token_id: str, window_min: int) -> Optional[float]:
    """Devuelve el precio mas antiguo dentro de la ventana (o None)."""
    if not token_id or not BASE44_API_KEY:
        return None
    try:
        cutoff = time.time() - window_min * 60
        resp = requests.get(
            _endpoint("MarketSnapshot"),
            headers={"api_key": BASE44_API_KEY},
            params={"token_id": token_id, "sort": "-snapshot_at", "limit": 40},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json() or []
        if isinstance(data, dict):
            data = data.get("data") or []
        import datetime as _dt
        oldest_price = None
        for s in data:
            t = s.get("snapshot_at") or ""
            try:
                ts = _dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if ts < cutoff:
                break
            p = s.get("price")
            if p is not None:
                oldest_price = float(p)
        return oldest_price
    except requests.RequestException:
        return None


class ContrarianFade:
    def __init__(self, order_manager=None, allocator: Optional[CapitalAllocator] = None):
        self.om = order_manager
        self.allocator = allocator or CapitalAllocator()
        self.seen_tokens: Dict[str, float] = {}  # token -> ts emitted

    def run_cycle(self):
        mode = self.allocator.get_execution_mode(STRATEGY)
        if mode == "disabled":
            return
        if mode == "live":
            logger.warning("contrarian_fade en live mode pero ejecutor live no implementado; skip.")
            return
        rec = self.allocator.get(STRATEGY) or {}
        cfg = rec.get("config") or {}
        min_move = float(cfg.get("min_move_pct") or MIN_MOVE_PCT)
        window_min = int(cfg.get("max_move_window_min") or MOVE_WINDOW_MIN)
        size_usdc = float(rec.get("trade_size_min") or POSITION_SIZE_USDC)

        markets = _fetch_active_markets()
        if not markets:
            return

        now = time.time()
        emitted = 0
        # Dedup: no re-emitir mismo token en 6h
        for m in markets:
            vol = 0.0
            try:
                vol = float(m.get("volume") or m.get("volumeNum") or 0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol < MIN_VOLUME_USDC:
                continue
            current = _extract_yes_price(m)
            tokens = _extract_token_ids(m)
            if current is None or not tokens:
                continue
            token_yes = tokens[0]
            last_emit = self.seen_tokens.get(token_yes, 0)
            if now - last_emit < 6 * 3600:
                continue
            # Precio hace window_min minutos
            previous = _fetch_recent_snapshot(token_yes, window_min)
            if previous is None or previous <= 0:
                continue
            move_pct = abs(current - previous) / previous * 100.0
            if move_pct < min_move:
                continue
            # Fade: entrar CONTRA la direccion del movimiento.
            # Si precio YES subio mucho -> fade vendiendo YES = comprar NO.
            # Pero paper_lab espera un "BUY" simple con token_id. Elegimos el outcome
            # contrario al movimiento para fade.
            if current > previous:
                # YES subio -> fade con NO
                fade_token = tokens[1] if len(tokens) > 1 else None
                fade_outcome = "NO"
                fade_price = 1.0 - current  # precio NO aproximado
            else:
                # YES bajo -> fade con YES (comprar barato)
                fade_token = token_yes
                fade_outcome = "YES"
                fade_price = current
            if not fade_token or not (0.02 < fade_price < 0.98):
                continue

            emit_paper_trade(
                strategy=STRATEGY,
                market=(m.get("question") or m.get("slug") or "?")[:300],
                side="BUY",
                entry_price=fade_price,
                size_usdc=size_usdc,
                token_id=fade_token,
                condition_id=m.get("conditionId") or m.get("id"),
                outcome=fade_outcome,
                tp_pct=TP_PCT,
                sl_pct=SL_PCT,
                max_hold_hours=MAX_HOLD_HOURS,
                features={
                    "prev_price": previous,
                    "current_price": current,
                    "move_pct": round(move_pct, 2),
                    "volume_24h": vol,
                    "window_min": window_min,
                },
                signal_meta={
                    "fade_direction": "against_up" if current > previous else "against_down",
                },
            )
            self.seen_tokens[token_yes] = now
            emitted += 1
            if emitted >= 3:
                break  # maximo 3 por ciclo

        if emitted:
            logger.info("contrarian_fade: %d paper trades emitidos", emitted)
