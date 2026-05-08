from __future__ import annotations

from typing import Any
import json
import requests

from config import Settings, settings as default_settings
from models import PolymarketMarket


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


class PolymarketPublicClient:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self.gamma_url = self.cfg.polymarket_gamma_url.rstrip('/')
        self.clob_url = self.cfg.polymarket_clob_url.rstrip('/')

    def fetch_active_markets(self, limit: int = 300) -> list[PolymarketMarket]:
        url = f'{self.gamma_url}/markets'
        params = {
            'active': 'true',
            'closed': 'false',
            'archived': 'false',
            'limit': limit,
            'order': 'volume',
            'ascending': 'false',
        }
        resp = requests.get(url, params=params, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        raw_markets = data.get('data') if isinstance(data, dict) else data
        if not isinstance(raw_markets, list):
            return []
        parsed = [self._parse_market(m) for m in raw_markets if isinstance(m, dict)]
        # Keep only markets with IDs/questions to avoid empty records.
        return [m for m in parsed if m.id and m.question]

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
