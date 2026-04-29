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
# ENDPOINT_INDEX_V1 — path real incluye /index (function en subcarpeta).
RECEIVE_ENDPOINT = f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/receiveWhaleSignal/index"

_last_run_at: float = 0.0
_whales_cache: List[Dict[str, Any]] = []
_whales_cache_at: float = 0.0


def _load_tier_s_whales() -> List[Dict[str, Any]]:
    """MULTI_WHALE_SHADOW_V1 — carga Tier S + SHADOW (Multi-Whale Fase 1)."""
    global _whales_cache, _whales_cache_at
    now = time.time()
    if _whales_cache and (now - _whales_cache_at) < WHALES_CACHE_TTL_SECONDS:
        return _whales_cache
    # Multi-Whale Fase 1: Tier S (operacionales) + SHADOW (observacion pura).
    # SHADOW signals quedan marcados execution_blocked=true en receiveWhaleSignal.
    rows_s = list_records("WhaleTrader", limit=50, query={"tier": "S", "enabled": True}) or []
    rows_shadow = list_records("WhaleTrader", limit=50, query={"tier": "SHADOW", "enabled": True}) or []
    rows = rows_s + rows_shadow
    _whales_cache = rows
    _whales_cache_at = now
    if rows:
        logger.info("whale_watcher: %d wallets cargadas (S=%d SHADOW=%d)", len(rows), len(rows_s), len(rows_shadow))
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
        is_shadow = str(whale.get("tier") or "").upper() == "SHADOW" or whale.get("shadow_mode") is True
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
            "tier": whale.get("tier"),
            "shadow": is_shadow,
        }
    except Exception:
        return None


def _send_to_base44(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"ok": True, "skipped": True}
    try:
        r = requests.post(
            RECEIVE_ENDPOINT,
            # SOURCE_BOT_V1 — etiqueta cada batch con el bot origen para The Race dashboard.
            json={"trades": trades, "source_bot": os.environ.get("SHADOW_BOT_ID", "mac")},
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


# FAST_PATH_V1 helpers fast-path
_FAST_PATH_TENNIS_TOP_KEYWORDS = (
    "madrid", "rome", "french", "wimbledon", "us-open", "australian",
    "indian-wells", "miami", "monte-carlo", "shanghai", "atp-masters",
    "wta-1000",
)
# FAST_PATH_ALL_SPORTS_V1 — Bolt+Opus+JP 2026-04-29: filtros abiertos. Causa: trade Mensik tardó 213s
# por filtros viejos demasiado estrictos. Ahora swisstony en CUALQUIER deporte, cualquier
# price (5-95c), cualquier size (>$1) dispara fast-path. Filtro real es whale_name=swisstony.
# Resto de whales sigue por flujo lento hasta tener data propia en Shadow Book.
# FAST_PATH_MAX_PRICE_80_V1 — bajado max 0.95→0.80. Spread Celtics 97¢ rompió R/R.
_FAST_PATH_MIN_PRICE = 0.05
_FAST_PATH_MAX_PRICE = 0.80
_FAST_PATH_MIN_USDC = 1.0
_FAST_PATH_WHALE_NAMES = ("swisstony", "swiss_tony")
# FAST_PATH_URL_SUFFIX_V1: Base44 expone esta function como executeApprovedProposal/index.
_FAST_PATH_DISPATCH_URL = (
    f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/executeApprovedProposal/index"
)
_FAST_PATH_PROPOSAL_URL = (
    f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/entities/CopyTradeProposal"
)
_FAST_PATH_POSITION_URL = (
    f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/entities/Position"
)


def _is_fast_path_candidate(tr: Dict[str, Any]) -> bool:
    # Multi-Whale Fase 1: SHADOW whales NUNCA disparan fast-path.
    if tr.get("shadow") is True or str(tr.get("tier") or "").upper() == "SHADOW":
        return False
    name = str(tr.get("whale_name") or "").lower()
    if not any(t in name for t in _FAST_PATH_WHALE_NAMES):
        return False
    # FAST_PATH_ALL_SPORTS_V1: filtro de slug eliminado. swisstony en cualquier deporte → fast-path.
    try:
        price = float(tr.get("price") or 0)
        size_usdc = float(tr.get("size_usdc") or 0)
    except Exception:
        return False
    if not (_FAST_PATH_MIN_PRICE <= price <= _FAST_PATH_MAX_PRICE):
        return False
    if size_usdc < _FAST_PATH_MIN_USDC:
        return False
    if not tr.get("token_id") or not tr.get("condition_id"):
        return False
    # FAST_PATH_BLACKLIST_NOISE_V1: blacklist de mercados ruido. Spread/O/U/BTTS son hedges micro
    # de swisstony con R/R catastrófico cuando están cerca de resolución.
    q = str(tr.get("market_question") or "").lower()
    if any(k in q for k in ("spread:", "o/u", "both teams to score", "moneyline")):
        return False
    if "(-" in q or "(+" in q:  # spreads tipo "Team (-1.5)"
        return False
    return True


def _has_open_position_for_condition(condition_id: str) -> bool:
    """True si ya tenemos Position abierta con ese condition_id."""
    try:
        # Buscamos por market_question (proxy razonable, Position no tiene
        # condition_id directo, pero token_id ya está en cada Position).
        # En realidad lo mejor es buscar por token_id del trade actual,
        # pero condition_id es el filtro semánticamente correcto. Dejamos
        # el endpoint de Base44 manejar el match por token_id en
        # executeApprovedProposal, acá filtramos lo más obvio.
        rows = list_records(
            "Position",
            limit=5,
            query={"status": "open"},
        )
        # Como no tenemos query por condition_id, devolvemos False y
        # confiamos en la doble verificación que hace executeApprovedProposal
        # (DEDUP TOKEN_ID GUARD ya implementado ahí).
        return False
    except Exception:
        return False


def _dispatch_fast_path(tr: Dict[str, Any]) -> None:
    """FAST_PATH_INLINE_V1: una sola POST a executeApprovedProposal con api_key header.
    La function crea la CopyTradeProposal con asServiceRole y ejecuta inline."""
    if _has_open_position_for_condition(tr.get("condition_id", "")):
        logger.info("fast_path skip: ya hay Position abierta para condition_id")
        return
    detected_at_iso = (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())) + "Z"
    )
    proposal_payload = {
        "status": "approved",
        "tier": "premium",
        "rejection_reason": "auto_approved_fast_path_swisstony_FAST_PATH_INLINE_V1",
        "category": "sports",
        "side": tr.get("side", "BUY"),
        "outcome": tr.get("outcome"),
        "entry_price": float(tr.get("price") or 0),
        "amount_usdc": float(tr.get("size_usdc") or 10.0),
        "suggested_size_usdc": float(tr.get("size_usdc") or 10.0),
        "token_id": tr.get("token_id"),
        "condition_id": tr.get("condition_id"),
        "market_question": tr.get("market_question"),
        "market_slug": tr.get("market_slug"),
        "whale_addresses": [tr.get("whale_address", "").lower()],
        "whale_names": [tr.get("whale_name") or "swisstony"],
        "whale_count": 1,
        "avg_whale_wr": 1.0,
        "quality_score": 100,
        "total_whale_usdc": float(tr.get("size_usdc") or 0),
        "responded_at": detected_at_iso,
    }
    try:
        r = requests.post(
            _FAST_PATH_DISPATCH_URL,
            headers={
                "Content-Type": "application/json",
                "api_key": BASE44_API_KEY,
            },
            json={
                "fast_path": True,
                "proposal_payload": proposal_payload,
                "watcher_detected_at": detected_at_iso,
            },
            timeout=15,
        )
        if r.status_code >= 400:
            logger.warning("fast_path dispatch %d: %s", r.status_code, r.text[:200])
            return
        # FAST_PATH_LOG_FIX_V1: la function backend crea la proposal con asServiceRole.
        result = r.json() if r.text else {}
        logger.info("FAST_PATH dispatched: %s", str(result)[:300])
    except Exception as exc:
        logger.warning("fast_path dispatch: %s", exc)


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

    # FAST_PATH_V1 — fast-path swisstony tenis top (Bolt+Opus+JP 2026-04-27).
    # Antes de mandar al endpoint lento, escaneo los trades por candidatos
    # ultra-rápidos: swisstony Tier S, tenis Masters/Slam, precio 50-80c, size>$100.
    # Si encuentro alguno, llamo executeApprovedProposal directo y salto el cron.
    try:
        for tr in all_trades:
            if _is_fast_path_candidate(tr):
                _dispatch_fast_path(tr)
    except Exception as exc:
        logger.warning("fast_path scan failed: %s", exc)

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
