from __future__ import annotations

from typing import Any
import json
import logging
import requests

from config import Settings, settings as default_settings
from models import PolymarketMarket, ExternalEvent

log = logging.getLogger(__name__)


def _safe_float(value, default=None):
    try:
        if value is None or value == '':
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _last_name(name: str) -> str:
    parts = [p for p in (name or '').replace('-', ' ').split() if p]
    return parts[-1] if parts else ''


def _event_queries(event: ExternalEvent) -> list[str]:
    home = event.home_team or ''
    away = event.away_team or ''
    queries = []
    if home and away:
        queries.extend([
            f'{home} {away}',
            f'{away} {home}',
            f'{_last_name(home)} {_last_name(away)}'.strip(),
            f'{_last_name(away)} {_last_name(home)}'.strip(),
        ])
    for q in [home, away, _last_name(home), _last_name(away)]:
        if q and len(q) >= 4:
            queries.append(q)
    seen = set()
    out = []
    for q in queries:
        q = ' '.join(q.split())
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:6]


class PolymarketPublicClient:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.gamma_url = self.cfg.polymarket_gamma_url.rstrip('/')
        self.clob_url = self.cfg.polymarket_clob_url.rstrip('/')

    def fetch_active_markets(self, limit: int = 300, offset: int = 0, search: str | None = None) -> list[PolymarketMarket]:
        url = f'{self.gamma_url}/markets'
        params = {
            'active': 'true',
            'closed': 'false',
            'archived': 'false',
            'limit': limit,
            'offset': offset,
            'order': 'volume',
            'ascending': 'false',
        }
        if search:
            # Gamma commonly accepts search-like query params. If unsupported,
            # the API simply returns a generic page and local matching still protects us.
            params['search'] = search
            params['q'] = search
        resp = requests.get(url, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        raw_markets = data.get('data') if isinstance(data, dict) else data
        if not isinstance(raw_markets, list):
            return []
        parsed = [self._parse_market(m) for m in raw_markets if isinstance(m, dict)]
        return [m for m in parsed if m.id and m.question]

    def fetch_markets_for_events(self, events: list[ExternalEvent], broad_limit: int = 300) -> list[PolymarketMarket]:
        """Fetch broad markets plus targeted searches around event participants.

        This is deliberately conservative: local mapper still requires both H2H
        participants, so extra generic markets do not become tradable signals.
        """
        by_id: dict[str, PolymarketMarket] = {}

        def add_many(items: list[PolymarketMarket]) -> None:
            for m in items:
                by_id.setdefault(m.id, m)

        # Broad discovery: several pages by volume, because sports markets may not
        # sit in the top 300 global markets at every moment.
        for offset in (0, broad_limit, broad_limit * 2):
            try:
                add_many(self.fetch_active_markets(limit=broad_limit, offset=offset))
            except Exception as exc:
                log.warning('polymarket broad fetch failed offset=%s err=%s', offset, exc)

        # Targeted discovery: only first 30 external events to protect Gamma/API rate.
        for event in events[:30]:
            for query in _event_queries(event):
                try:
                    add_many(self.fetch_active_markets(limit=50, offset=0, search=query))
                except Exception as exc:
                    log.debug('polymarket targeted fetch failed query=%s err=%s', query, exc)

        return list(by_id.values())

    def _parse_market(self, m: dict[str, Any]) -> PolymarketMarket:
        token_ids = _json_list(m.get('clobTokenIds'))
        outcomes = _json_list(m.get('outcomes'))
        prices = _json_list(m.get('outcomePrices'))
        best_bid = _safe_float(m.get('bestBid'))
        best_ask = _safe_float(m.get('bestAsk'))
        if (best_bid is None or best_ask is None) and prices:
            p = _safe_float(prices[0])
            if p is not None and 0 < p < 1:
                best_bid = max(0.01, p - 0.01)
                best_ask = min(0.99, p + 0.01)
        midpoint = None
        spread = None
        if best_bid is not None and best_ask is not None and best_ask > best_bid:
            midpoint = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
        return PolymarketMarket(
            id=str(m.get('id') or m.get('conditionId') or m.get('slug') or ''),
            question=str(m.get('question') or m.get('title') or m.get('slug') or ''),
            slug=str(m.get('slug') or ''),
            category=str(m.get('category') or m.get('eventCategory') or ''),
            start_date=m.get('startDate') or m.get('start_date_iso'),
            end_date=m.get('endDate') or m.get('end_date_iso') or m.get('endDateIso'),
            condition_id=m.get('conditionId'),
            yes_token_id=str(token_ids[0]) if len(token_ids) > 0 else None,
            no_token_id=str(token_ids[1]) if len(token_ids) > 1 else None,
            outcomes=[str(x) for x in outcomes],
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            liquidity=float(_safe_float(m.get('liquidity') or m.get('liquidityNum'), 0.0) or 0.0),
            raw_payload=m,
        )
