"""paper_broker.py - Broker simulado para modo paper trading (DRY_RUN).

Implementa la misma interfaz publica que OrderManager:
  - connect()
  - place_limit_buy(token_id, price, size, market_id, strategy)
  - close_position_market(token_id, size, market_id, strategy)
  - refresh(...)  (no-op, para que main.py no rompa)

Mantiene un balance ficticio en memoria (PAPER_CAPITAL_USDC). No toca la
Safe ni el ClobClient. Reporta cada fill simulado como Trade (status="paper")
a Base44 y registra el PnL realizado al cerrar.
"""

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import (
    BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL,
    PAPER_CAPITAL_USDC,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _b44_headers():
    return {"api_key": BASE44_API_KEY or "", "Content-Type": "application/json"}


def _b44_trade_url(record_id: Optional[str] = None) -> str:
    base = "%s/api/apps/%s/entities/Trade" % (BASE44_BASE_URL, BASE44_APP_ID)
    return "%s/%s" % (base, record_id) if record_id else base


def _b44_create_trade(payload: Dict[str, Any]) -> Optional[str]:
    if not BASE44_API_KEY:
        return None
    try:
        resp = requests.post(_b44_trade_url(), json=payload,
                             headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("paper trade create %d: %s",
                           resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return data.get("id") or (data.get("data") or {}).get("id")
    except requests.RequestException as exc:
        logger.error("paper trade create fallo: %s", exc)
        return None


def _b44_update_trade(record_id: str, payload: Dict[str, Any]) -> None:
    if not record_id or not BASE44_API_KEY:
        return
    try:
        requests.patch(_b44_trade_url(record_id), json=payload,
                       headers=_b44_headers(), timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.error("paper trade update fallo: %s", exc)


class PaperBroker:
    """Broker simulado. Interfaz compatible con OrderManager."""

    def __init__(self):
        self.client = None
        self.creds = None
        self.starting_balance = float(PAPER_CAPITAL_USDC)
        self.cash = float(PAPER_CAPITAL_USDC)
        # positions[token_id] = {"size": tokens, "entry_price": float,
        #                        "market_id": str, "strategy": str,
        #                        "record_id": str, "opened_at": float}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self._orders: Dict[str, Dict[str, Any]] = {}
        # stats diarios (se resetean en paper_daily_report.reset_day())
        self.wins_today = 0
        self.losses_today = 0
        self.pnl_today = 0.0
        self.pnl_total = 0.0
        self.max_equity = float(PAPER_CAPITAL_USDC)
        self.max_drawdown_usdc = 0.0
        # tracker fake para compat con modulos que lo consultan
        self.tracker = _FakeTracker(self)

    # ------------------------------------------------------------ lifecycle
    def connect(self):
        logger.info(
            "[PAPER] PaperBroker activo. Balance ficticio=%.2f USDC. "
            "No se ejecutan ordenes reales en la CLOB.",
            self.starting_balance,
        )

    # ---------------------------------------------------------------- state
    def equity(self, mark_prices: Optional[Dict[str, float]] = None) -> float:
        """Equity total: cash + valor de posiciones abiertas al mark price.

        Si no hay mark_prices disponibles, valua al entry_price (proxy conservador).
        """
        mark = mark_prices or {}
        value = self.cash
        for tok, pos in self.positions.items():
            px = mark.get(tok, pos["entry_price"])
            value += px * pos["size"]
        return value

    def _touch_drawdown(self) -> None:
        eq = self.equity()
        if eq > self.max_equity:
            self.max_equity = eq
        dd = self.max_equity - eq
        if dd > self.max_drawdown_usdc:
            self.max_drawdown_usdc = dd

    # --------------------------------------------------------------- orders
    def place_limit_buy(self, token_id: str, price: float, size: float,
                        market_id: str = "", strategy: str = "") -> List[str]:
        """Simula fill inmediato al precio limite."""
        if size <= 0 or price <= 0:
            return []
        notional = float(price) * float(size)
        if notional > self.cash + 1e-6:
            logger.warning("[PAPER] insufficient cash: need=%.2f have=%.2f",
                           notional, self.cash)
            return []

        oid = "paper-%s" % uuid.uuid4().hex[:12]
        self.cash -= notional

        existing = self.positions.get(token_id)
        if existing:
            new_size = existing["size"] + size
            new_entry = ((existing["entry_price"] * existing["size"]) +
                         (price * size)) / new_size
            existing["size"] = new_size
            existing["entry_price"] = new_entry
        else:
            record_id = _b44_create_trade({
                "market": market_id or token_id,
                "side": "BUY",
                "entry_price": float(price),
                "size_usdc": notional,
                "strategy": strategy or "paper",
                "status": "paper",
                "entry_time": _iso_now(),
                "notes": "paper_trade simulated fill",
            })
            self.positions[token_id] = {
                "size": float(size),
                "entry_price": float(price),
                "market_id": market_id,
                "strategy": strategy,
                "record_id": record_id,
                "opened_at": time.time(),
            }

        self._orders[oid] = {
            "token_id": token_id, "price": price, "size": size,
            "strategy": strategy, "market_id": market_id,
            "created_at": time.time(),
        }
        self._touch_drawdown()
        logger.info("[PAPER] BUY fill tok=%s px=%.4f size=%.2f notional=%.2f cash=%.2f",
                    token_id[:10], price, size, notional, self.cash)
        return [oid]

    def close_position_market(self, token_id: str, size: float,
                              market_id: str = "", strategy: str = "",
                              exit_price: Optional[float] = None) -> bool:
        pos = self.positions.get(token_id)
        if not pos:
            return False
        close_size = min(float(size), pos["size"])
        # Sin book real: asumimos exit al entry (flat) salvo que el caller
        # pase exit_price (caso estrategias con target/stop).
        px = float(exit_price) if exit_price is not None else pos["entry_price"]
        proceeds = px * close_size
        cost = pos["entry_price"] * close_size
        pnl = proceeds - cost
        self.cash += proceeds
        pos["size"] -= close_size

        self.pnl_today += pnl
        self.pnl_total += pnl
        if pnl >= 0:
            self.wins_today += 1
        else:
            self.losses_today += 1

        if pos.get("record_id"):
            _b44_update_trade(pos["record_id"], {
                "exit_price": float(px),
                "pnl": float(pnl),
                "pnl_pct": float(pnl / cost) if cost else 0.0,
                "status": "closed",
                "exit_time": _iso_now(),
                "notes": "paper_trade simulated close",
            })

        logger.info("[PAPER] CLOSE tok=%s px=%.4f size=%.2f pnl=%+.2f cash=%.2f",
                    token_id[:10], px, close_size, pnl, self.cash)

        if pos["size"] <= 1e-6:
            self.positions.pop(token_id, None)
        self._touch_drawdown()
        return True

    # ---------------------------------------------------------- market data
    def refresh(self, *args, **kwargs):
        """No-op: el OrderManager real recicla ordenes stale / repinta MM.
        En paper el fill es inmediato, no hay ordenes colgadas.
        """
        return

    def get_open_orders(self, *args, **kwargs) -> List[Dict[str, Any]]:
        return []

    def get_active_markets(self) -> List[str]:
        return list({p["market_id"] for p in self.positions.values() if p.get("market_id")})

    def cancel_all(self) -> int:
        n = len(self._orders)
        self._orders.clear()
        return n

    # --------------------------------------------------------------- stats
    def snapshot(self) -> Dict[str, Any]:
        total = self.wins_today + self.losses_today
        win_rate = (self.wins_today / total * 100.0) if total else 0.0
        return {
            "mode": "paper",
            "starting_balance": self.starting_balance,
            "cash": round(self.cash, 4),
            "equity": round(self.equity(), 4),
            "open_positions": len(self.positions),
            "pnl_today": round(self.pnl_today, 4),
            "pnl_total": round(self.pnl_total, 4),
            "wins_today": self.wins_today,
            "losses_today": self.losses_today,
            "win_rate_pct": round(win_rate, 2),
            "max_drawdown_usdc": round(self.max_drawdown_usdc, 4),
        }


class _FakeTracker:
    """Tracker-shape compatible para modulos que consultan om.tracker."""

    def __init__(self, broker: "PaperBroker"):
        self._broker = broker

    def list_open(self) -> List[Dict[str, Any]]:
        return [
            {"token_id": tok, "size": p["size"],
             "entry_price": p["entry_price"],
             "market_id": p.get("market_id", ""),
             "strategy": p.get("strategy", "")}
            for tok, p in self._broker.positions.items()
        ]

    def register_buy(self, *args, **kwargs):
        # La logica real de registro ya ocurre en place_limit_buy.
        return

    def check_and_close(self, *args, **kwargs):
        return
