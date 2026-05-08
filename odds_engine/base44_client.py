from __future__ import annotations

from typing import Any
import requests

from config import settings
from models import to_dict


class Base44Client:
    def __init__(self) -> None:
        self.base_url = settings.base44_api_url.rstrip('/')
        self.token = settings.base44_api_token
        self.enabled = bool(self.base_url and self.token)

    def post_record(self, entity: str, item: Any) -> bool:
        if not self.enabled:
            return False
        url = f'{self.base_url}/records/{entity}'
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }
        try:
            resp = requests.post(url, json=to_dict(item), headers=headers, timeout=10)
            return 200 <= resp.status_code < 300
        except Exception:
            return False


base44 = Base44Client()
