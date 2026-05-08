from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from models import to_dict, BotLog, now_iso
from config import settings


class JsonlStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def append(self, stream: str, item: Any) -> None:
        path = self.data_dir / f'{stream}.jsonl'
        record = to_dict(item)
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')

    def log(self, level: str, source: str, message: str, data: dict | None = None) -> None:
        self.append('bot_logs', BotLog(level=level, source=source, message=message, data=data or {}, created_at=now_iso()))


store = JsonlStore()
