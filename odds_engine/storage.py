from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from models import to_dict, BotLog, now_iso
from config import settings

log = logging.getLogger(__name__)


class JsonlStore:
    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sent_path = self.data_dir / 'base44_sent_keys.json'
        self._sent_cache: set[str] | None = None

    def append(self, stream: str, item: Any) -> None:
        path = self.data_dir / f'{stream}.jsonl'
        record = to_dict(item)
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        if stream == 'papertrade':
            self._refresh_paper_portfolio_after_trade()

    def _refresh_paper_portfolio_after_trade(self) -> None:
        """Best-effort local portfolio refresh after each paper trade append.

        This intentionally does not raise: a failed mark/summary must never
        prevent storing the trade itself or crash the odds worker.
        """
        try:
            from paper_portfolio_refresh import refresh_paper_portfolio
            refresh_paper_portfolio(settings)
        except Exception as exc:
            log.exception('paper_portfolio_refresh_failed err=%s', exc)

    def _load_sent(self) -> set[str]:
        if self._sent_cache is not None:
            return self._sent_cache
        if not self.sent_path.exists():
            self._sent_cache = set()
            return self._sent_cache
        try:
            data = json.loads(self.sent_path.read_text())
            self._sent_cache = set(data if isinstance(data, list) else [])
        except Exception:
            self._sent_cache = set()
        return self._sent_cache

    def base44_was_sent(self, key: str) -> bool:
        return key in self._load_sent()

    def mark_base44_sent(self, key: str) -> None:
        sent = self._load_sent()
        if key in sent:
            return
        sent.add(key)
        tmp = self.sent_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(sorted(sent), ensure_ascii=False))
        tmp.replace(self.sent_path)

    def log(self, level: str, source: str, message: str, data: dict | None = None) -> None:
        self.append('bot_logs', BotLog(level=level, source=source, message=message, data=data or {}, created_at=now_iso()))


store = JsonlStore()
