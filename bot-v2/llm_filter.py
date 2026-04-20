"""llm_filter.py - Filtro de oportunidades via Claude (Anthropic Messages API).

Flujo:
  market_scanner -> lista de oportunidades -> filter_opportunities(opps, strategy)
  -> Claude responde JSON {approve, reason} por cada oportunidad -> se
  devuelven solo las aprobadas.

Requiere ANTHROPIC_API_KEY en el entorno. Si falta o la API falla, la
funcion es fail-open (retorna la lista tal cual) para no bloquear el bot.

Cache en memoria (TTL 5 min) por (question, strategy) para no gastar tokens
repitiendo el mismo mercado cada ciclo.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 300  # 5 min

# (question, strategy) -> (ts, approve, reason)
_CACHE: Dict[Tuple[str, str], Tuple[float, bool, str]] = {}


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _hours_to_resolution(market: Dict[str, Any]) -> Optional[float]:
    from datetime import datetime, timezone
    raw = market.get("endDate") or market.get("endDateIso")
    if not raw:
        return None
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 3600.0
    except (ValueError, TypeError):
        return None


def _build_prompt(opp: Dict[str, Any], strategy: str) -> str:
    raw = opp.get("raw") or {}
    question = opp.get("question") or raw.get("question") or opp.get("market_id") or "?"
    mid = _safe_float(opp.get("mid"))
    bid = _safe_float(opp.get("bid"))
    ask = _safe_float(opp.get("ask"))
    spread_pct = _safe_float(opp.get("spread_pct"))
    volume = _safe_float(opp.get("volume") or raw.get("volumeNum"))
    liquidity = _safe_float(opp.get("liquidity") or raw.get("liquidityNum"))
    hours_left = _hours_to_resolution(raw)
    days_left = (hours_left / 24.0) if hours_left is not None else None

    yes_price = mid
    no_price = (1.0 - mid) if mid > 0 else 0.0

    lines = [
        "Evalua esta oportunidad de trading en Polymarket.",
        "",
        "Mercado: " + str(question),
        "Estrategia: " + str(strategy),
        "Precio YES: %.3f" % yes_price,
        "Precio NO:  %.3f" % no_price,
        "Mid: %.3f | Bid: %.3f | Ask: %.3f" % (mid, bid, ask),
        "Spread: %.2f%%" % (spread_pct * 100.0),
        "Volumen: %.0f USDC" % volume,
        "Liquidez: %.0f USDC" % liquidity,
    ]
    if days_left is not None:
        lines.append("Dias a resolucion: %.1f" % days_left)
    lines += [
        "",
        "Decide si aprobar el trade considerando:",
        "- Coherencia del precio con la pregunta (no entrar en eventos obvios)",
        "- Riesgo de resolucion abrupta (noticias, eventos proximos)",
        "- Calidad del mercado (volumen y liquidez suficientes)",
        "- Si la estrategia tiene sentido para este mercado",
        "",
        "Responde SOLO con un JSON valido, sin markdown:",
        '{"approve": true_o_false, "reason": "motivo en <=25 palabras"}',
    ]
    return "\n".join(lines)


def _parse_response(text: str) -> Tuple[bool, str]:
    """Extrae JSON del texto. Tolerante a fences markdown."""
    if not text:
        return True, "empty_response_fail_open"
    s = text.strip()
    # Quitar fences si el modelo los pone.
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
    # Encontrar primer { y ultimo }.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return True, "parse_fail_open"
    try:
        obj = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return True, "json_fail_open"
    approve = bool(obj.get("approve", True))
    reason = str(obj.get("reason") or "")[:200]
    return approve, reason


def _call_claude(prompt: str, api_key: str) -> Optional[str]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(ANTHROPIC_URL, headers=headers, json=body,
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("llm_filter: request fallo: %s", exc)
        return None
    if resp.status_code >= 400:
        logger.warning("llm_filter: HTTP %d: %s",
                       resp.status_code, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    blocks = data.get("content") or []
    for b in blocks:
        if b.get("type") == "text":
            return b.get("text") or ""
    return None


def evaluate(opp: Dict[str, Any], strategy: str) -> Tuple[bool, str]:
    """Evalua una oportunidad. Retorna (approve, reason).

    Fail-open: si no hay ANTHROPIC_API_KEY o la API falla, aprueba.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return True, "no_api_key_fail_open"

    question = opp.get("question") or (opp.get("raw") or {}).get("question") or ""
    cache_key = (str(question), str(strategy))
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1], cached[2]

    prompt = _build_prompt(opp, strategy)
    text = _call_claude(prompt, api_key)
    if text is None:
        # Fail-open pero NO cacheamos para reintentar el siguiente ciclo.
        return True, "api_fail_open"

    approve, reason = _parse_response(text)
    _CACHE[cache_key] = (now, approve, reason)
    return approve, reason


def filter_opportunities(opps: List[Dict[str, Any]], strategy: str) -> List[Dict[str, Any]]:
    """Filtra una lista de oportunidades via Claude.

    - Cada opp es un dict con 'question', 'mid', 'spread_pct', etc.
    - Las rechazadas se loggean con el motivo.
    - Fail-open: si no hay API key, devuelve la lista tal cual.
    """
    if not opps:
        return opps
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return opps

    approved: List[Dict[str, Any]] = []
    rejected = 0
    for opp in opps:
        approve, reason = evaluate(opp, strategy)
        if approve:
            approved.append(opp)
        else:
            rejected += 1
            q = (opp.get("question") or opp.get("market_id") or "?")[:60]
            logger.info("llm_filter REJECT [%s] %s :: %s", strategy, q, reason)
    if rejected:
        logger.info("llm_filter [%s]: %d aprobadas, %d rechazadas (total=%d)",
                    strategy, len(approved), rejected, len(opps))
    return approved
