from __future__ import annotations

from typing import Any
import logging
import requests

from config import settings
from models import to_dict

log = logging.getLogger(__name__)


class Base44Client:
    """Thin client for the Base44 app entity API.

    This engine is allowed to write dashboard/audit data only. It never sends
    Polymarket live orders. If Base44 credentials are missing, the engine keeps
    running and stores JSONL locally.
    """

    def __init__(self) -> None:
        self.base_url = settings.base44_base_url.rstrip('/')
        self.api_key = settings.base44_api_key
        self.app_id = settings.base44_app_id
        self.enabled = bool(self.base_url and self.api_key and self.app_id)

    def _endpoint(self, entity: str) -> str:
        return f'{self.base_url}/api/apps/{self.app_id}/entities/{entity}'

    def _headers(self) -> dict[str, str]:
        return {
            'api_key': self.api_key,
            'x-api-key': self.api_key,
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
            'User-Agent': 'independent-odds-engine-v1',
        }

    def post_record(self, entity: str, item: Any) -> dict | None:
        if not self.enabled:
            return None
        payload = to_dict(item)
        try:
            resp = requests.post(self._endpoint(entity), json=payload, headers=self._headers(), timeout=15)
            if resp.status_code >= 400:
                log.warning('Base44 POST %s failed %s: %s', entity, resp.status_code, resp.text[:300])
                return None
            try:
                return resp.json()
            except ValueError:
                return {'status': 'ok'}
        except requests.RequestException as exc:
            log.warning('Base44 POST %s exception: %s', entity, exc)
            return None

    def list_records(self, entity: str, limit: int = 50, sort: str | None = None) -> list[dict]:
        if not self.enabled:
            return []
        params: dict[str, Any] = {'limit': limit}
        if sort:
            params['sort'] = sort
        try:
            resp = requests.get(self._endpoint(entity), headers=self._headers(), params=params, timeout=15)
            if resp.status_code >= 400:
                log.warning('Base44 GET %s failed %s: %s', entity, resp.status_code, resp.text[:300])
                return []
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get('records') or data.get('data') or []
            return []
        except (requests.RequestException, ValueError) as exc:
            log.warning('Base44 GET %s exception: %s', entity, exc)
            return []

    def fetch_bot_config(self) -> dict:
        records = self.list_records('BotConfig', limit=1, sort='-updated_date')
        if not records:
            return {}
        rec = records[0]
        if isinstance(rec.get('data'), dict):
            merged = dict(rec['data'])
            merged.update({k: v for k, v in rec.items() if k != 'data'})
            return merged
        return rec


base44 = Base44Client()
