"""base44_client.py - Cliente HTTP para escribir en Base44."""

import json
import logging
from datetime import datetime, timezone

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _endpoint(entity):
    return "%s/api/apps/%s/entities/%s" % (BASE44_BASE_URL, BASE44_APP_ID, entity)


def _headers():
    return {"api_key": BASE44_API_KEY, "Content-Type": "application/json"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_record(entity, payload):
    if not BASE44_API_KEY:
        logger.debug("BASE44_API_KEY vacio; omitiendo %s", entity)
        return None
    # LogEvent.data en la app externa es string; si viene dict, serializar.
    if entity == "LogEvent" and isinstance(payload, dict):
        raw = payload.get("data")
        if isinstance(raw, (dict, list)):
            payload = dict(payload)
            try:
                payload["data"] = json.dumps(raw, default=str)
            except (TypeError, ValueError):
                payload["data"] = str(raw)
    try:
        resp = requests.post(_endpoint(entity), json=payload,
                             headers=_headers(), timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.error("Base44 %s %d: %s", entity, resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok"}
    except requests.RequestException as exc:
        logger.error("Fallo Base44 %s: %s", entity, exc)
        return None


# --- portfolio_sync helpers ---
def _base_entity_url(entity):
    return "%s/api/apps/%s/entities/%s" % (BASE44_BASE_URL, BASE44_APP_ID, entity)


def list_records(entity, sort="-created_date", limit=100):
    """Lista registros de una entidad. Devuelve [] ante cualquier fallo."""
    if not BASE44_API_KEY:
        return []
    url = _base_entity_url(entity)
    params = {"sort": sort, "limit": limit}
    try:
        resp = requests.get(url, params=params, headers=_headers(),
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Base44 list %s %d: %s",
                           entity, resp.status_code, resp.text[:200])
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("data") or data.get("records") or []
    except requests.RequestException as exc:
        logger.warning("Base44 list %s fallo: %s", entity, exc)
        return []


def update_record(entity, record_id, patch):
    """Actualiza un registro por id (PUT; fallback PATCH si 404/405)."""
    if not BASE44_API_KEY or not record_id:
        return None
    url = "%s/%s" % (_base_entity_url(entity), record_id)
    for method in ("PUT", "PATCH"):
        try:
            resp = requests.request(method, url, json=patch,
                                    headers=_headers(),
                                    timeout=REQUEST_TIMEOUT)
            if resp.status_code in (404, 405) and method == "PUT":
                continue
            if resp.status_code >= 400:
                logger.error("Base44 update %s %d: %s",
                             entity, resp.status_code, resp.text[:200])
                return None
            try:
                return resp.json()
            except ValueError:
                return {"status": "ok"}
        except requests.RequestException as exc:
            logger.error("Fallo update Base44 %s/%s: %s",
                         entity, record_id, exc)
            return None
    return None
