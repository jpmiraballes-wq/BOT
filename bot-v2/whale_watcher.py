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
from collections import deque
from typing import Any, Dict, List, Optional

import json
import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL
from base44_client import list_records

# WHALE_WATCHER_TRI_PATCH_20260430 P2 DEDUP_CACHE_PERSIST_V1: cache en disco para sobrevivir restarts.
_DEDUP_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".dedup_cache.json")


def _load_dedup_cache() -> Dict[str, float]:
    """Carga _condition_last_exec desde disco. Si no existe o falla, dict vacío."""
    try:
        with open(_DEDUP_CACHE_PATH, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
        return {str(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def _save_dedup_cache() -> None:
    """Persiste _condition_last_exec en disco. Limpia entradas vencidas (>60min)."""
    try:
        now = time.time()
        clean = {
            k: v for k, v in _condition_last_exec.items()
            if now - v < _FAST_PATH_DEDUP_60MIN_SECONDS
        }
        with open(_DEDUP_CACHE_PATH, "w") as f:
            json.dump(clean, f)
    except Exception as e:
        logger.warning("dedup_cache save failed (path=%s): %s", _DEDUP_CACHE_PATH, e)

logger = logging.getLogger("whale_watcher")

WHALE_INTERVAL_SECONDS = int(os.environ.get("WHALE_INTERVAL_SECONDS", "30"))
WHALES_CACHE_TTL_SECONDS = 10 * 60
DATA_API_BASE = "https://data-api.polymarket.com"
# ENDPOINT_INDEX_V1 — path real incluye /index (function en subcarpeta).
RECEIVE_ENDPOINT = f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/receiveWhaleSignal/index"

_last_run_at: float = 0.0
_whales_cache: List[Dict[str, Any]] = []
_whales_cache_at: float = 0.0

# SWISSTONY_DEDICATED_LANE_V1: carril dedicado para swisstony, independiente del loop general 30s.
# Corre cada 3s y deduplica localmente por transactionHash para no tocar cloud dos veces.
_SWISSTONY_LANE_INTERVAL_SECONDS = int(os.environ.get("SWISSTONY_LANE_INTERVAL_SECONDS", "3"))
_SWISSTONY_TX_CACHE_MAX = 500
_last_swisstony_lane_at: float = 0.0
_seen_tx_hashes = deque(maxlen=_SWISSTONY_TX_CACHE_MAX)
_seen_tx_hash_set = set()


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


def _is_swisstony_whale(whale: Dict[str, Any]) -> bool:
    name = str(whale.get("display_name") or "").lower()
    return "swisstony" in name or "swiss_tony" in name


def _load_swisstony_whale() -> Optional[Dict[str, Any]]:
    for whale in _load_tier_s_whales():
        if _is_swisstony_whale(whale) and whale.get("enabled") is True:
            return whale
    return None


def _remember_swisstony_tx(tx_hash: str) -> bool:
    """SWISSTONY_DEDICATED_LANE_V1: True si es hash nuevo; False si ya lo vimos localmente."""
    global _seen_tx_hashes, _seen_tx_hash_set
    if not tx_hash:
        return False
    if tx_hash in _seen_tx_hash_set:
        return False
    if len(_seen_tx_hashes) >= _SWISSTONY_TX_CACHE_MAX:
        old = _seen_tx_hashes.popleft()
        _seen_tx_hash_set.discard(old)
    _seen_tx_hashes.append(tx_hash)
    _seen_tx_hash_set.add(tx_hash)
    return True


# FETCH_WALLET_TRADES_LIMIT_100: bump 30→100. Wallets activas (surfandturf 100+ trades/día) perdían trades frescos.
def _fetch_wallet_trades(address: str, limit: int = 100) -> List[Dict[str, Any]]:
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


def _is_bot_paused() -> bool:
    """KILL_SWITCH_FAST_PATH_V1: si BotConfig.paused/emergency_stop está activo, no crear proposals."""
    try:
        cfgs = list_records("BotConfig", limit=1)
        cfg = (cfgs or [{}])[0]
        return bool(cfg.get("paused") is True or cfg.get("emergency_stop") is True)
    except Exception as exc:
        logger.warning("fast_path_kill_switch_check_failed: %s", str(exc)[:120])
        return True  # fail closed: si no podemos leer BotConfig, no disparamos fast-path


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
# DYNAMIC_TRUSTED_WHALES_V1: lista hardcoded como fallback. La fuente real es la cloud
# (endpoint getTrustedWhalesList) que se consulta cada 60s. Cuando una shadow
# promueve a Tier S vía whaleAutoPromoteShadow, el bot la pica solo en el
# próximo ciclo sin push.
_FAST_PATH_WHALE_NAMES_FALLBACK = ("swisstony", "swiss_tony", "surfandturf")
_TRUSTED_CACHE: tuple = _FAST_PATH_WHALE_NAMES_FALLBACK
_TRUSTED_CACHE_AT: float = 0.0
_TRUSTED_TTL_SECONDS = 60
_TRUSTED_ENDPOINT = f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/getTrustedWhalesList/index"


def _load_trusted_whales() -> tuple:
    """Lee Tier S whales desde la cloud cada 60s. Fallback a hardcoded."""
    global _TRUSTED_CACHE, _TRUSTED_CACHE_AT
    now = time.time()
    if _TRUSTED_CACHE and (now - _TRUSTED_CACHE_AT) < _TRUSTED_TTL_SECONDS:
        return _TRUSTED_CACHE
    try:
        resp = requests.get(
            _TRUSTED_ENDPOINT,
            headers={"api_key": BASE44_API_KEY},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            whales = data.get("whales") if isinstance(data, dict) else None
            if isinstance(whales, list) and len(whales) > 0:
                tokens = tuple(str(w).lower().replace("_", "").replace(" ", "").strip() for w in whales if w)
                if tokens:
                    _TRUSTED_CACHE = tokens
                    _TRUSTED_CACHE_AT = now
                    return tokens
        logger.warning("trusted_whales fetch failed status=%s, using cache/fallback", resp.status_code)
    except Exception as e:
        logger.warning("trusted_whales fetch error: %s, using cache/fallback", e)
    # Si nunca cargó nada → fallback hardcoded
    if not _TRUSTED_CACHE:
        _TRUSTED_CACHE = _FAST_PATH_WHALE_NAMES_FALLBACK
    _TRUSTED_CACHE_AT = now
    return _TRUSTED_CACHE


# Compat: el resto del archivo usa _FAST_PATH_WHALE_NAMES como tuple. Lo
# convertimos en property-like via __getattr__ del módulo seria overkill;
# preferimos resolver dentro de la función que usa el chequeo.
_FAST_PATH_WHALE_NAMES = _FAST_PATH_WHALE_NAMES_FALLBACK  # legacy ref, no se usa más
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
# FAST_PATH_CONDITION_LOCKOUT_V1: lockout 60s por condition_id. Evita que el bot abra 4 posiciones
# del mismo partido en 5 min cuando swisstony va flippeando precios.
_FAST_PATH_LOCKOUT_SECONDS = 60
_fast_path_recent: Dict[str, float] = {}
# DEDUP_CONDITION_60MIN_V2: ventana 60min por condition_id, dict independiente del lockout 60s.
# Suma defensa contra dupes cuando swisstony flippea el mismo mercado en minutos.
# Si el bot reinicia, la cloud (executeApprovedProposal DEDUP_CONDITION_24H)
# sigue cubriendo. Acá agregamos un layer extra in-process.
_FAST_PATH_DEDUP_60MIN_SECONDS = 3600
# WHALE_WATCHER_TRI_PATCH_20260430 P2 DEDUP_CACHE_PERSIST_V1: dict cargado desde disco al arrancar.
_condition_last_exec: Dict[str, float] = _load_dedup_cache()

# ITF_CHALLENGER_QUALS_ABSOLUTE_BLOCK_MAC_V1 (JP+Opus 2026-05-05): bloqueo absoluto sin bypass.
# Roma Qualifications, ITF, Challenger, Futures, Jiujiang/Wuxi quedan fuera
# aunque swisstony meta $230-$899. Fast-path no debe ni llamar cloud.
_FAST_PATH_ITF_CHALLENGER_QUALS_BLOCK_TERMS = (
    "challenger", "itf", "futures", "qualifying", "qualification", "qualifications", "qualifier",
    "jiujiang", "wuxi", "saint-malo", "saint malo", "mauthausen", "la bisbal",
    "aix en provence", "aix-en-provence", "cagliari", "ostrava", "francavilla",
    "antalya", "tallahassee", "meerbusch", "shymkent", "savannah", "oeiras", "bonita springs",
)


def _run_swisstony_lane_once() -> Dict[str, Any]:
    """SWISSTONY_DEDICATED_LANE_V1: poll dedicado de swisstony cada 3s, con dedupe local por tx hash."""
    whale = _load_swisstony_whale()
    if not whale:
        return {"ok": False, "reason": "swisstony_not_found"}
    addr = (whale.get("wallet_address") or "").lower()
    if not addr:
        return {"ok": False, "reason": "swisstony_missing_address"}

    raw_trades = _fetch_wallet_trades(addr, limit=100)
    _ts_cutoff = time.time() - 700
    dispatched = 0
    seen = 0
    for raw in raw_trades:
        tx_hash = raw.get("transactionHash") or raw.get("tx_hash") or raw.get("id")
        if not tx_hash or not _remember_swisstony_tx(str(tx_hash)):
            seen += 1
            continue
        try:
            if float(raw.get("timestamp") or 0) <= _ts_cutoff:
                continue
        except Exception:
            continue
        norm = _normalize_trade(raw, whale)
        if not norm or not norm.get("trade_hash"):
            continue
        # SWISSTONY_MIRROR_MODE_V1_WATCHER: mirror completo de Swisstony.
        # BUY puede disparar fast-path; SELL se manda inmediatamente al cloud
        # para que position_tp_sl cierre nuestra Position del mismo token.
        # MIRROR_WATCHER_KILLED_V1_2026_05_06 (JP+Opus+Bolt): segundo loop de
        # mirror que escapaba al pushKillMirrorV2. Caso Noguchi 6-may 12:56:
        # Position cerrada 10min después de abrir, partido EN JUEGO, close_reason=
        # 'no_balance_on_chain' pnl=$0 (real -$3.97). Forzamos a False para que
        # los SELL del whale ya NO disparen mirror. Las BUYs siguen igual.
        # SHADOW_PURE_MIRROR_SELL_PROPORTIONAL_V1 (JP+Opus 2026-05-06):
        # resucitado del MIRROR_WATCHER_KILLED_V1. Calculamos sell_pct contra el
        # balance previo del whale en este token (sumando BUYs/SELLs anteriores
        # del cache raw_trades) y lo mandamos en el payload. El cloud lo registra
        # en la WhaleSignal; el bot Python lo usa en _find_recent_swisstony_sell.
        # Si no hay history previo o falla → sell_pct=1.0 (full close, safe default).
        # Aceptado riesgo bug Noguchi pnl=$0/no_balance_on_chain — auditoría aparte.
        if str(norm.get("side") or "").upper() == "SELL":
            try:
                sell_size = float(norm.get("size_tokens") or norm.get("size") or 0)
                tok = str(norm.get("token_id") or norm.get("asset") or "")
                cur_ts = float(raw.get("timestamp") or 0)
                prior_balance = 0.0
                if sell_size > 0 and tok and cur_ts > 0:
                    for prior in raw_trades:
                        if prior is raw:
                            continue
                        if str(prior.get("asset") or prior.get("token_id") or "") != tok:
                            continue
                        prior_ts = float(prior.get("timestamp") or 0)
                        if prior_ts <= 0 or prior_ts >= cur_ts:
                            continue
                        ps = str(prior.get("side") or "").upper()
                        psize = float(prior.get("size") or 0)
                        if ps == "BUY":
                            prior_balance += psize
                        elif ps == "SELL":
                            prior_balance -= psize
                sell_pct = 1.0
                if prior_balance > 0.01:
                    sell_pct = min(1.0, sell_size / prior_balance)
                norm["sell_pct"] = round(sell_pct, 4)
                norm["mirror_kind"] = "swisstony_proportional"
                logger.info(
                    "SHADOW_PURE_MIRROR_SELL_PROPORTIONAL_V1: token=%s sell_size=%.2f prior=%.2f pct=%.4f",
                    tok[-12:], sell_size, prior_balance, sell_pct
                )
            except Exception as e:
                logger.warning("SHADOW_PURE_MIRROR_SELL_PROPORTIONAL_V1 calc failed: %s — defaulting sell_pct=1.0", e)
                norm["sell_pct"] = 1.0
                norm["mirror_kind"] = "swisstony_proportional"
            _send_to_base44([norm])
            dispatched += 1
            continue
        if _is_fast_path_candidate(norm):
            _dispatch_fast_path(norm)
            dispatched += 1

    return {
        "ok": True,
        "wallet": "swisstony",
        "raw": len(raw_trades),
        "duplicates_seen": seen,
        "dispatched": dispatched,
    }


def maybe_run_swisstony_lane() -> Optional[Dict[str, Any]]:
    """SWISSTONY_DEDICATED_LANE_V1: llamado en cada loop principal; no espera WHALE_INTERVAL_SECONDS."""
    global _last_swisstony_lane_at
    now = time.time()
    if now - _last_swisstony_lane_at < _SWISSTONY_LANE_INTERVAL_SECONDS:
        return None
    _last_swisstony_lane_at = now
    try:
        return _run_swisstony_lane_once()
    except Exception as exc:
        logger.warning("swisstony_lane fallo: %s", exc, exc_info=True)
        return None


def _is_fast_path_candidate(tr: Dict[str, Any]) -> bool:
    # Multi-Whale Fase 1: SHADOW whales NUNCA disparan fast-path.
    if tr.get("shadow") is True or str(tr.get("tier") or "").upper() == "SHADOW":
        return False
    name = str(tr.get("whale_name") or "").lower()
    if not any(t in name for t in _load_trusted_whales()):  # DYNAMIC_TRUSTED_WHALES_V1
        return False
    # FAST_PATH_ALL_SPORTS_V1: filtro de slug eliminado. swisstony en cualquier deporte → fast-path.
    try:
        price = float(tr.get("price") or 0)
        size_usdc = float(tr.get("size_usdc") or 0)
    except Exception:
        return False
    # SWISSTONY_MIRROR_AGGRESSIVE_MAC_V1: espejo agresivo de Swisstony.
    # ITF/Challenger/Qualification solo pasan si es Swisstony y ticket >= $200.
    # Tickets chicos o whales no-Swisstony siguen bloqueados.
    _noise_text = " ".join(str(tr.get(k) or "") for k in ("market_slug", "market_question", "question", "title")).lower()
    _is_swisstony = ("swisstony" in name) or ("swiss_tony" in name)
    _big_conviction = _is_swisstony and size_usdc >= 200
    if any(term in _noise_text for term in _FAST_PATH_ITF_CHALLENGER_QUALS_BLOCK_TERMS) and not _big_conviction:
        logger.info(
            "fast_path_itf_challenger_quals_min_200_block: whale=%s size=$%.2f market=%s",
            name, size_usdc, (_noise_text[:120] or "unknown"),
        )
        return False
    if any(term in _noise_text for term in _FAST_PATH_ITF_CHALLENGER_QUALS_BLOCK_TERMS) and _big_conviction:
        logger.warning(
            "SWISSTONY_MIRROR_AGGRESSIVE_MAC_V1: passing big ITF/quals ticket whale=%s size=$%.2f market=%s",
            name, size_usdc, (_noise_text[:120] or "unknown"),
        )
    if not (_FAST_PATH_MIN_PRICE <= price <= _FAST_PATH_MAX_PRICE):
        return False
    if size_usdc < _FAST_PATH_MIN_USDC:
        return False
    if not tr.get("token_id") or not tr.get("condition_id"):
        return False
    # RESOLUTION_TOO_FAR_48H_MAC_V1 (JP+Opus 2026-04-30): bloquea mercados que resuelven a >48h.
    # Razón: swisstony tiene edge en near-term. Mercados lejanos = round-trip drena.
    # Bypass: swisstony con $200+ (favoritos lejanos de convicción son su edge).
    # Coherente con RESOLUTION_TOO_FAR_48H_V1 en cloud (executeApprovedProposal).
    if not _big_conviction:
        try:
            _cid = tr.get("condition_id")
            _gamma_url = f"https://gamma-api.polymarket.com/markets?condition_ids={_cid}"
            _gamma_resp = requests.get(_gamma_url, timeout=3)
            _end_iso = None
            if _gamma_resp.status_code == 200:
                _gamma_data = _gamma_resp.json()
                if isinstance(_gamma_data, list) and len(_gamma_data) > 0:
                    _end_iso = (_gamma_data[0] or {}).get("endDate")
            if not _end_iso:
                logger.info(
                    "fast_path_resolution_block: whale=%s slug=%s — no endDate from gamma-api",
                    tr.get("whale_name"), tr.get("market_slug"),
                )
                return False
            from datetime import datetime, timezone
            _end_dt = datetime.fromisoformat(_end_iso.replace("Z", "+00:00"))
            _hours_to_resolve = (_end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
            if _hours_to_resolve > 48:
                logger.info(
                    "fast_path_resolution_block: whale=%s slug=%s — resolves in %.1fh (>48h horizon)",
                    tr.get("whale_name"), tr.get("market_slug"), _hours_to_resolve,
                )
                return False
        except Exception as e:
            # Si gamma-api falla (red/timeout), pasamos: cloud filter atrapa igual.
            logger.warning("fast_path_resolution_check_error: %s", str(e)[:80])
    # FAST_PATH_AGE_GUARD_V2 (JP+Opus 2026-04-29 18:15): bloquea trades viejos (>5min) o sin ts.
    # Causa: data-api devuelve ultimos 30 trades sin filtro de fecha. Si la wallet
    # no opero hoy, devuelve trades viejos sobre mercados resueltos -> CLOB no_price
    # -> proposals fallan en loop. NBA-MIN-DEN de hace 37h pasaba.
    try:
        ts_candidates = [tr.get("whale_trade_ts"), tr.get("trade_ts"), tr.get("timestamp")]
        trade_ts = 0
        for c in ts_candidates:
            try:
                v = int(c or 0)
                if v > 0:
                    trade_ts = v
                    break
            except Exception:
                continue
        age_s = (time.time() - trade_ts) if trade_ts > 0 else -1
        # FAST_PATH_AGE_GUARD_600: bump 300s→600s. Atletico-Arsenal BTTS blocked at age_s=313.
        if trade_ts <= 0 or age_s > 600:
            logger.info(
                "fast_path_age_guard skip: whale=%s slug=%s ts=%s age_s=%s",
                tr.get("whale_name"), tr.get("market_slug"), trade_ts, int(age_s),
            )
            return False
    except Exception as exc:
        logger.warning("fast_path_age_guard error: %s", exc)
        return False
    # FAST_PATH_ITF_BLACKLIST_V1 (JP+Opus 2026-04-29 17:55): bloquea ITF / Challenger / Futures /
    # qualifying / torneos basura ANTES del POST. Mismo regex que el quality_gate
    # de la cloud (executeApprovedProposal). Causa: Saint-Malo Costoulas vs
    # Jeanjean entró 4 veces en 2 min porque el Mac crea Position inline antes
    # del gate cloud. Sangrado real ~$15+. Estricto: NO se bypassea swisstony
    # acá (la Opción B vive en cloud, donde se ve size acumulado).
    _ITF_NOISE_TOKENS = (
        "challenger", "itf", "futures", "qualifying", "qualifier",
        "jiujiang", "saint-malo", "saint malo", "mauthausen", "la bisbal",
        "aix en provence", "aix-en-provence", "cagliari", "ostrava",
        "francavilla", "antalya", "tallahassee", "meerbusch", "shymkent",
        "savannah", "oeiras", "bonita springs",
    )
    _itf_text = f"{(tr.get('market_question') or '').lower()} {(tr.get('market_slug') or '').lower()}"
    if any(token in _itf_text for token in _ITF_NOISE_TOKENS) and not _big_conviction:
        logger.info(
            "fast_path_itf_block: whale=%s slug=%s — ITF/Challenger blacklist (cloud-equivalent)",
            tr.get("whale_name"), tr.get("market_slug"),
        )
        return False
    # FAST_PATH_HARD_FILTER_V1: 4 reglas duras (mismo que cron whaleDetectConsensus).
    # Sangrado prod 03:30: Yokohama x4 en 90s, Recoleta, Cruzeiro draw, Cuenca 82c, Auxerre.
    q = str(tr.get("market_question") or "").lower()
    slug = str(tr.get("market_slug") or "").lower()
    text = q + " " + slug
    # Regla 1: precio max 75c (R/R catastrófico cerca de resolución).
    # FAST_PATH_MAX_PRICE_STRICT_V1: estricto. Antes era > 0.75 y dejaba pasar Napoli a 75¢ exacto.
    # SWISSTONY_CONVICTION_BYPASS_MAC_V1: bypass si swisstony con $200+ (favoritos claros tipo 80-97¢ son su edge).
    if price >= 0.75 and not _big_conviction:
        return False
    # Regla 2: blacklist Spread/O/U/BTTS/draw/moneyline
    if any(k in q for k in ("spread:", "o/u", "over/under", "both teams to score", "btts", "end in a draw", "moneyline")):
        return False
    if "(-" in q or "(+" in q:
        return False
    # Regla 3: tenis sin Challenger/ITF/Futures/qualifying
    tennis_kw = ("atp", "wta", "tennis", "grand-slam", "wimbledon", "us-open", "roland", "madrid open", "miami open")
    if any(k in text for k in tennis_kw):
        if any(k in text for k in ("challenger", "itf", "futures", "qualifying", "qualifier")) and not _big_conviction:
            return False
    # FAST_PATH_NBA_ABSOLUTE_BLACKLIST_V1: NBA blacklist ABSOLUTO. Alineado con cloud EXEC_NBA_RX (sin excepciones).
    # JP+Opus 2026-04-30 noche: Mac disparó Raptors x3 con whitelist surfandturf →
    # cloud bloqueaba pero el wallet quedaba comprado igual (fast-path crea Position directo).
    # Filosofía: si cloud bloquea, Mac también. Cero hueco entre Mac y cloud.
    nba_kw = ("nba", "lakers", "celtics", "warriors", "bucks", "76ers", "sixers", "heat", "knicks", "nets", "raptors", "bulls", "cavaliers", "cavs", "pistons", "pacers", "hawks", "hornets", "magic", "wizards", "thunder", "rockets", "spurs", "mavericks", "mavs", "grizzlies", "pelicans", "nuggets", "jazz", "timberwolves", "trail blazers", "blazers", "kings", "suns", "clippers")
    if any(k in text for k in nba_kw):
        return False
    # Regla 4: soccer/football solo top-5 EU + size>=$300 (bloquea J-League, sudam, MLS, Saudi)
    soccer_kw = (" fc", "fc ", "soccer", "football", "win on 20", "marinos", "recoleta", "cruzeiro", "boca juniors", "auxerre", "cuenca", "barracas", "millonarios", "sao paulo", "tolima", "coquimbo", "audax")
    is_soccer = any(k in text for k in soccer_kw) and not any(k in text for k in ("nfl", "afc ", "nba", "mlb", "nhl"))
    if is_soccer:
        top5_eu = ("epl", "premier league", "la liga", "laliga", "bundesliga", "serie a", "ligue 1", "uefa champions", "champions league", "europa league")
        if not any(k in text for k in top5_eu):
            return False
        # WHALE_WATCHER_TRI_PATCH_20260430 P1 SOCCER_THRESHOLD_50_V1: $300 → $50 (top-5 EU ya filtrado arriba).
        if size_usdc < 50:
            return False
    return True


def _has_open_position_for_condition(condition_id: str) -> bool:
    """WHALE_WATCHER_TRI_PATCH_20260430 P3 HAS_OPEN_POSITION_REAL_V1: chequea condition_id/token_id real.

    Antes devolvía siempre False y dejaba el guard a la cloud. Ahora bloquea
    in-process antes de dispatchar el fast-path, ahorrando un round-trip.
    """
    if not condition_id:
        return False
    # FAST_PATH_POSITION_QUERY_FIX_V1 — JP+Opus 2026-05-02
    # Antes traía las 50 Positions abiertas globales y filtraba en Python. Si
    # había >50 Positions abiertas (caso noche 2026-05-02: 30+ entries a
    # PSG/Brentford/Villarreal por bug DEDUP_BYPASS_5X), la Position duplicada
    # podía caer fuera del slice y la función devolvía False → loop infinito
    # de entradas al mismo mercado.
    # Fix: query server-side por condition_id. limit=1 alcanza.
    try:
        rows = list_records(
            "Position",
            limit=1,
            query={"status": "open", "condition_id": condition_id},
        )
        if rows:
            return True
        # Fallback por token_id — Positions viejas pueden no tener condition_id
        # poblado (ver schema Position.condition_id: backfill desde 2026-05-01).
        rows_token = list_records(
            "Position",
            limit=1,
            query={"status": "open", "token_id": condition_id},
        )
        return bool(rows_token)
    except Exception as exc:
        # Fail-OPEN solo si la API falla. Si responde y dice "no hay" → False
        # legítimo. Cloud DEDUP_24H sigue siendo el último filtro.
        logger.warning("has_open_position query error: %s — fail-open", exc)
        return False


def _dispatch_fast_path(tr: Dict[str, Any]) -> None:
    """FAST_PATH_INLINE_V1: una sola POST a executeApprovedProposal con api_key header.
    La function crea la CopyTradeProposal con asServiceRole y ejecuta inline."""
    if _is_bot_paused():
        logger.warning(
            "FAST_PATH_KILL_SWITCH: BotConfig paused/emergency_stop active — skip proposal whale=%s market=%s",
            tr.get("whale_name"),
            (tr.get("market_question") or tr.get("market_slug") or "")[:120],
        )
        return
    cid = tr.get("condition_id", "")
    # FAST_PATH_CONDITION_LOCKOUT_V1: lockout 60s por condition_id.
    now_ts = time.time()
    last_dispatch = _fast_path_recent.get(cid, 0.0)
    if cid and (now_ts - last_dispatch) < _FAST_PATH_LOCKOUT_SECONDS:
        logger.info(
            "fast_path_lockout_60s skip: condition_id=%s last=%.1fs ago",
            cid[:12], now_ts - last_dispatch,
        )
        return
    # DEDUP_CONDITION_60MIN_V2: segundo guard, ventana 60min por condition_id.
    last_exec_60min = _condition_last_exec.get(cid, 0.0)
    if cid and (now_ts - last_exec_60min) < _FAST_PATH_DEDUP_60MIN_SECONDS:
        logger.info(
            "DEDUP_60MIN blocked: condition_id=%s last=%ds ago",
            cid[:12], int(now_ts - last_exec_60min),
        )
        return
    if _has_open_position_for_condition(cid):
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
        "amount_usdc": float(tr.get("size_usdc") or 25.0),
        "suggested_size_usdc": float(tr.get("size_usdc") or 25.0),
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
    # DEDUP_PRE_POST_V1 (JP+Opus 2026-05-04): set del cache ANTES del POST para
    # que ráfagas in-process del mismo cid no pasen aunque el POST tarde o falle
    # silencioso al persistir. Si el POST falla con HTTP >=400, hacemos rollback
    # explícito para no bloquear reintentos legítimos. Timeout/excepción NO
    # rollbackean (mejor bloquear 60min que duplicar si el POST llegó al cloud).
    if cid:
        _fast_path_recent[cid] = now_ts
        _condition_last_exec[cid] = now_ts
        _save_dedup_cache()
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
            # DEDUP_PRE_POST_V1 rollback: el dispatch falló, liberamos el lock
            # para que un retry legítimo pueda pasar.
            if cid:
                _fast_path_recent.pop(cid, None)
                _condition_last_exec.pop(cid, None)
                _save_dedup_cache()
            logger.warning("fast_path dispatch %d: %s", r.status_code, r.text[:200])
            return
        result = r.json() if r.text else {}
        # Cleanup paralelo del dict 60min (más laxo, cutoff 2x ventana).
        if cid:
            if len(_condition_last_exec) > 500:
                cutoff_60min = now_ts - (_FAST_PATH_DEDUP_60MIN_SECONDS * 2)
                for k in list(_condition_last_exec.keys()):
                    if _condition_last_exec[k] < cutoff_60min:
                        del _condition_last_exec[k]
            # Garbage collect: borra entradas viejas (>5min) para no crecer infinito.
            if len(_fast_path_recent) > 500:
                cutoff = now_ts - 300
                for k in list(_fast_path_recent.keys()):
                    if _fast_path_recent[k] < cutoff:
                        del _fast_path_recent[k]
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
        # FETCH_WALLET_TRADES_LIMIT_500_V1: bump 30→500 en call site (el default=100 no se usaba) +
        # filtro client-side 700s. surfandturf con limit=100 seguía trayendo
        # trades viejos (UFC 25-abr) porque hace 100+ trades/día. 500 captura
        # ~24h de actividad incluso para wallets más activas. El filtro 700s
        # descarta ruido viejo antes del fast-path (que tiene age_guard 600s).
        raw_trades = _fetch_wallet_trades(addr, limit=500)
        _ts_cutoff = time.time() - 700
        raw_trades = [t for t in raw_trades if float(t.get("timestamp") or 0) > _ts_cutoff]
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
    """Llamado en cada loop. Swisstony corre por carril 3s; el resto sigue cada 30s."""
    global _last_run_at

    # SWISSTONY_DEDICATED_LANE_V1: carril dedicado, no bloquea ni altera el loop general de 30s.
    maybe_run_swisstony_lane()

    now = time.time()
    if now - _last_run_at < WHALE_INTERVAL_SECONDS:
        return None
    _last_run_at = now

    try:
        return run_whale_watcher_once()
    except Exception as exc:
        logger.error("whale_watcher fallo: %s", exc, exc_info=True)
        return None
