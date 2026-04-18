"""base44_client.py - Cliente HTTP para escribir en Base44."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _endpoint(entity):
    return "%s/api/apps/%s/entities/%s/records" % (BASE44_BASE_URL, BASE44_APP_ID, entity)


def _headers():
    return {"api_key": BASE44_API_KEY, "Content-Type": "application/json"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_record(entity, payload):
    if not BASE44_API_KEY:
        logger.debug("BASE44_API_KEY vacio; omitiendo %s", entity)
        return None
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
