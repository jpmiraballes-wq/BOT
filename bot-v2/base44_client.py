"""
base44_client.py — Cliente HTTP fino para escribir en entidades de Base44.

Centraliza POST/GET/PUT a /api/apps/<APP_ID>/entities/<Entity>/records con
la API key, para que módulos como reporter.py, decision_logger.py,
order_manager.py o position_tp_sl.py puedan registrar/leer eventos sin
duplicar código.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15


def _endpoint(entity: str) -> str:
    return f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/entities/{entity}/records"


def _headers() -> Dict[str, str]:
    return {
        "api_key": BASE44_API_KEY,
        "Content-Type": "application/json",
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_record(entity: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Crea un registro en la entidad indicada. Devuelve la respuesta o None."""
    if not BASE44_API_KEY:
        logger.debug("BASE44_API_KEY vacío; omitiendo escritura en %s", entity)
        return None
    try:
        resp = requests.post(
            _endpoint(entity),
            json=payload,
            headers=_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("Base44 %s %d: %s",
                         entity, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok"}
    except requests.RequestException as exc:
        logger.error("Fallo al escribir en Base44 %s: %s", entity, exc)
        return None


def list_records(entity: str, limit: int = 1,
                 query: Optional[Dict[str, Any]] = None,
                 sort: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Lista registros de una entidad. Devuelve lista o [] en error.

    Args:
        entity: nombre de la entidad.
        limit: máximo de registros a devolver.
        query: dict de filtros a aplicar (igualdad). Ej: {"status": "open"}.
        sort: campo de ordenación, '-campo' para descendente.
    """
    if not BASE44_API_KEY:
        return []
    try:
        params: Dict[str, Any] = {"limit": limit}
        if sort:
            params["sort"] = sort
        if query:
            for k, v in query.items():
                params[k] = "true" if v is True else ("false" if v is False else v)
        resp = requests.get(
            _endpoint(entity),
            headers=_headers(),
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code >= 400:
            logger.error("Base44 GET %s %d: %s",
                         entity, resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except (requests.RequestException, ValueError) as exc:
        logger.error("Fallo al leer Base44 %s: %s", entity, exc)
        return []


def update_record(entity: str, record_id: str,
                  payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Actualiza un registro específico. Devuelve la respuesta o None."""
    if not BASE44_API_KEY or not record_id:
        return None
    url = f"{_endpoint(entity)}/{record_id}"
    try:
        resp = requests.put(url, json=payload, headers=_headers(),
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.error("Base44 PUT %s/%s %d: %s",
                         entity, record_id, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok"}
    except requests.RequestException as exc:
        logger.error("Fallo al actualizar Base44 %s/%s: %s",
                     entity, record_id, exc)
        return None


def send_telegram(text: str) -> bool:
    """Envía un mensaje por Telegram. Silencioso si faltan credenciales."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        return resp.status_code < 400
    except requests.RequestException as exc:
        logger.error("Fallo Telegram: %s", exc)
        return False
