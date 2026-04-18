"""bot_config_reader.py - Lee el BotConfig mas reciente desde Base44.

Expone fetch_bot_config() que devuelve un dict con los campos relevantes
(paused, emergency_stop, ...) o {} si falla.
"""

import logging
from typing import Any, Dict

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 10


def fetch_bot_config() -> Dict[str, Any]:
    if not BASE44_API_KEY:
        return {}
    url = "%s/api/apps/%s/entities/BotConfig" % (BASE44_BASE_URL, BASE44_APP_ID)
    params = {"sort": "-created_date", "limit": 1}
    headers = {"api_key": BASE44_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("BotConfig fetch %d: %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json()
        records = data if isinstance(data, list) else (data.get("data") or data.get("records") or [])
        if records:
            return records[0] or {}
        return {}
    except requests.RequestException as exc:
        logger.warning("BotConfig fetch fallo: %s", exc)
        return {}
