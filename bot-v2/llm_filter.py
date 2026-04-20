"""llm_filter.py - Filtro de oportunidades via invokeLLMProxy (Base44).

Llama a la funcion invokeLLMProxy del dashboard (que internamente usa
InvokeLLM con creditos de Base44). Soporta response_json_schema asi el
modelo devuelve JSON valido sin necesidad de parsear fences markdown.

Requiere EXTERNAL_BASE44_API_KEY en el entorno (ya usado por otras partes
del bot). Si falta o la API falla, fail-open (aprueba).

Cache en memoria (TTL 5 min) por (question, strategy).
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

BASE44_APP_ID = os.getenv("BASE44_APP_ID", "69e189f649c5d21cd42536bc")
BASE44_BASE_URL = os.getenv("BASE44_BASE_URL", "https://app.base44.com")
PROXY_URL = "%s/api/apps/%s/functions/invokeLLMProxy" % (
    BASE44_BASE_URL, BASE44_APP_ID,
)

# claude_sonnet_4_6 = Claude Sonnet, balance calidad/costo.
# Fallback automatico si la primera llamada falla.
PRIMARY_MODEL = os.getenv("LLM_FILTER_MODEL", "claude_sonnet_4_6")
FALLBACK_MODEL = "gpt_5_mini"

REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 300

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["approve", "reason"],
}

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
        "Respuesta: approve (bool) y reason (<=25 palabras).",
    ]
    return "\n".join(lines)


def _call_proxy(prompt: str, api_key: str, model: str) -> Optional[Dict[str, Any]]:
    """POST a invokeLLMProxy. Devuelve el dict con approve/reason o None."""
    headers = {"api_key": api_key, "Content-Type": "application/json"}
    body = {
        "prompt": prompt,
        "response_json_schema": RESPONSE_SCHEMA,
        "model": model,
    }
    try:
        resp = requests.post(PROXY_URL, headers=headers, json=body,
                             timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("llm_filter: request fallo (%s): %s", model, exc)
        return None
    if resp.status_code >= 400:
        logger.warning("llm_filter: HTTP %d (%s): %s",
                       resp.status_code, model, resp.text[:200])
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    # invokeLLMProxy envuelve en {result: ...}
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict) and "approve" in result:
        return result
    logger.warning("llm_filter: respuesta inesperada (%s): %s",
                   model, str(data)[:200])
    return None


def evaluate(opp: Dict[str, Any], strategy: str) -> Tuple[bool, str]:
    """Evalua una oportunidad. Retorna (approve, reason).

    Fail-open: si no hay API key o el proxy falla en ambos modelos, aprueba.
    """
    api_key = os.getenv("EXTERNAL_BASE44_API_KEY", "").strip()
    if not api_key:
        return True, "no_api_key_fail_open"

    question = opp.get("question") or (opp.get("raw") or {}).get("question") or ""
    cache_key = (str(question), str(strategy))
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
        return cached[1], cached[2]

    prompt = _build_prompt(opp, strategy)

    # Intento con modelo primario (claude_sonnet_4_6).
    result = _call_proxy(prompt, api_key, PRIMARY_MODEL)
    # Fallback a gpt_5_mini si el primario falla.
    if result is None:
        logger.info("llm_filter: fallback a %s", FALLBACK_MODEL)
        result = _call_proxy(prompt, api_key, FALLBACK_MODEL)
    if result is None:
        return True, "api_fail_open"

    approve = bool(result.get("approve", True))
    reason = str(result.get("reason") or "")[:200]
    _CACHE[cache_key] = (now, approve, reason)
    return approve, reason


def filter_opportunities(opps: List[Dict[str, Any]], strategy: str) -> List[Dict[str, Any]]:
    """Filtra oportunidades via LLM. Fail-open si falta API key."""
    if not opps:
        return opps
    api_key = os.getenv("EXTERNAL_BASE44_API_KEY", "").strip()
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
