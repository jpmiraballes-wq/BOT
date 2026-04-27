"""whale_watcher.py — Polling rápido de Tier S wallets desde la Mac.

Bolt+Opus 2026-04-27 noche. Plan "Overtake Swisstony" MOV 2.

Cada 30s (default), polea Polymarket data API para los Tier S whales y manda
trades nuevos al endpoint Base44 `receiveWhaleSignal` que se encarga de:
  - Dedupe por trade_hash.
  - Cálculo de detection_lag_seconds = now - whale_trade_ts.
  - Crear WhaleSignal en DB.

NO escribe directo en DB — pasa por endpoint para preservar el flujo del LagMonitor.

Las direcciones Tier S vienen del entity WhaleTrader filtrado por tier='S' AND enabled=true.
Se cachean 10 minutos.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL
from base44_client import list_records

logger = logging.getLogger("whale_watcher")

WHALE_INTERVAL_SECONDS = int(os.environ.get("WHALE_INTERVAL_SECONDS", "30"))
WHALES_CACHE_TTL_SECONDS = 10 * 60
DATA_API_BASE = "https://data-api.polymarket.com"
RECEIVE_ENDPOINT = f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/receiveWhaleSignal"

_last_run_at: float = 0.0
_whales_cache: List[Dict[str, Any]] = []
_whales_cache_at: float = 0.0


def _load_tier_s_whales() -> List[Dict[str, Any]]:
    global _whales_cache, _whales_cache_at
    now = time.time()
    if _whales_cache and (now - _whales_cache_at) < WHALES_CACHE_TTL_SECONDS:
        return _whales_cache
    # Carga whales tier=S enabled=true
    rows = list_records("WhaleTrader", limit=50, query={"tier": "S", "enabled": True})
    _whales_cache = rows or []
    _whales_cache_at = now
    if rows:
        logger.info("whale_watcher: %d Tier S wallets cargadas", len(rows))
    return _whales_cache


def _fetch_wallet_trades(address: str, limit: int = 30) -> List[Dict[str, Any]]:
    try:
        r = requests.get(
            f"{DATA_API_BASE}/trades",
            params={"user": address, "limit": limit},
            timeout=8,
            headers={"User-Agent": "opus-whale-watcher/1.0"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return data
    except Exception as exc:
        logger.warning("fetch_wallet_trades %s: %s", address[:8], exc)
        return []


def _normalize_trade(raw: Dict[str, Any], whale: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normaliza trade del data-api al formato que espera receiveWhaleSignal."""
    try:
        side = (raw.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            return None
        price = float(raw.get("price") or 0)
        size = float(raw.get("size") or 0)
        if price <= 0 or size <= 0:
            return None
        return {
            "whale_address": whale.get("wallet_address", "").lower(),
            "whale_name": whale.get("display_name") or whale.get("wallet_address", "")[:8],
            "trade_hash": raw.get("transactionHash") or raw.get("tx_hash") or raw.get("id"),
            "market_slug": raw.get("slug"),
            "market_question": raw.get("title") or raw.get("question"),
            "condition_id": raw.get("conditionId") or raw.get("condition_id"),
            "token_id": raw.get("asset") or raw.get("token_id"),
            "outcome": raw.get("outcome"),
            "side": side,
            "price": price,
            "size_tokens": size,
            "size_usdc": price * size,
            "whale_trade_ts": int(raw.get("timestamp") or 0),
        }
    except Exception:
        return None


def _send_to_base44(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"ok": True, "skipped": True}
    try:
        r = requests.post(
            RECEIVE_ENDPOINT,
            json={"trades": trades},
            headers={
                "api_key": BASE44_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        if r.status_code >= 400:
            logger.warning("receiveWhaleSignal %d: %s", r.status_code, r.text[:200])
            return {"ok": False, "status": r.status_code}
        return r.json()
    except Exception as exc:
        logger.error("send_to_base44: %s", exc)
        return {"ok": False, "error": str(exc)}


def run_whale_watcher_once() -> Dict[str, Any]:
    """Ejecuta una corrida: poll todas las wallets Tier S, manda nuevos trades."""
    started = time.time()
    whales = _load_tier_s_whales()
    if not whales:
        return {"ok": False, "reason": "no_tier_s_whales"}

    all_trades: List[Dict[str, Any]] = []
    for w in whales:
        addr = (w.get("wallet_address") or "").lower()
        if not addr:
            continue
        raw_trades = _fetch_wallet_trades(addr, limit=30)
        for raw in raw_trades:
            norm = _normalize_trade(raw, w)
            if norm and norm.get("trade_hash"):
                all_trades.append(norm)

    if not all_trades:
        return {"ok": True, "wallets": len(whales), "trades_found": 0}

    result = _send_to_base44(all_trades)
    duration = time.time() - started
    created = result.get("created", 0) if isinstance(result, dict) else 0
    if created > 0:
        logger.info(
            "whale_watcher: %d wallets, %d trades, %d nuevos (%.1fs)",
            len(whales), len(all_trades), created, duration,
        )
    return {
        "ok": True,
        "wallets": len(whales),
        "trades_found": len(all_trades),
        "trades_new": created,
        "duration_s": round(duration, 2),
    }


def maybe_run_whale_watcher() -> Optional[Dict[str, Any]]:
    """Llamado en cada loop. Solo ejecuta si pasaron WHALE_INTERVAL_SECONDS."""
    global _last_run_at
    now = time.time()
    if now - _last_run_at < WHALE_INTERVAL_SECONDS:
        return None
    _last_run_at = now

    try:
        return run_whale_watcher_once()
    except Exception as exc:
        logger.error("whale_watcher fallo: %s", exc, exc_info=True)
        return None
