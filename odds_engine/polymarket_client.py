from __future__ import annotations

from typing import Any
import json
import logging
import re
import unicodedata
from collections import defaultdict

import requests
from rapidfuzz import fuzz

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


def _normalize(value: str) -> str:
    s = unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _last_name(name: str) -> str:
    parts = [p for p in (name or '').replace('-', ' ').split() if p]
    return parts[-1] if parts else ''


def _team_tokens(name: str) -> list[str]:
    text = _normalize(name)
    stop = {'fc', 'cf', 'club', 'the', 'de', 'la', 'los', 'las'}
    return [p for p in text.split() if len(p) >= 3 and p not in stop]


def _event_queries(event: ExternalEvent) -> list[str]:
    home = event.home_team or ''
    away = event.away_team or ''
    home_last = _last_name(home)
    away_last = _last_name(away)
    queries = []
    if home and away:
        queries.extend([
            f'{home} {away}',
            f'{away} {home}',
            f'{home} vs {away}',
            f'{away} vs {home}',
            f'{home_last} {away_last}'.strip(),
            f'{away_last} {home_last}'.strip(),
        ])
    for token in _team_tokens(home)[:2] + _team_tokens(away)[:2]:
        queries.append(token)
    seen = set()
    out = []
    for q in queries:
        q = ' '.join(q.split())
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:8]


def _contains_name(name: str, text: str) -> bool:
    n = _normalize(name)
    if not n:
        return False
    wrapped = f' {text} '
    if f' {n} ' in wrapped:
        return True
    parts = [p for p in n.split() if len(p) >= 3]
    if parts and f' {parts[-1]} ' in wrapped:
        return True
    return max(fuzz.partial_ratio(n, text), fuzz.token_set_ratio(n, text)) >= 88


def _market_relevant_to_event(event: ExternalEvent, market: PolymarketMarket) -> bool:
    text = _normalize(' '.join([market.question, market.slug, market.category, ' '.join(market.outcomes or [])]))
    return _contains_name(event.home_team, text) and _contains_name(event.away_team, text)


def _looks_like_sports_market(market: PolymarketMarket) -> bool:
    text = _normalize(' '.join([market.question, market.slug, market.category, ' '.join(market.outcomes or [])]))
    sports_terms = [
        'ufc', 'mma', 'fight', 'boxing', 'soccer', 'football', 'premier',
        'champions league', 'la liga', 'serie a', 'nba', 'nfl', 'mlb', 'nhl',
        'tennis', 'atp', 'wta', 'world cup', 'reds', 'astros', 'yankees',
        'dodgers', 'mets', 'cubs', 'padres', 'phillies', 'braves', 'rockies',
        'orioles', 'athletics', 'blue jays', 'angels', 'red sox', 'rays',
        'marlins', 'nationals', 'guardians', 'twins', 'white sox', 'mariners',
    ]
    non_sports_terms = [
        'election', 'senedd', 'market cap', 'ipo', 'fed', 'bitcoin', 'ethereum',
        'trump', 'president', 'temperature', 'weather', 'movie', 'album',
    ]
    if any(x in text for x in non_sports_terms):
        return False
    return any(x in text for x in sports_terms)


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
            params['search'] = search
            params['q'] = search
        resp = requests.get(url, params=params, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        raw_markets = data.get('data') if isinstance(data, dict) else data
        if not isinstance(raw_markets, list):
            return []
        parsed = [self._parse_market(m) for m in raw_markets if isinstance(m, dict)]
        return [m for m in parsed if m.id and m.question]

    def fetch_markets_for_events(self, events: list[ExternalEvent], broad_limit: int | None = None) -> list[PolymarketMarket]:
        broad_limit = int(broad_limit or self.cfg.polymarket_broad_limit)
        by_id: dict[str, PolymarketMarket] = {}
        calls = 0
        targeted_raw = 0
        targeted_kept = 0
        broad_raw = 0
        broad_kept = 0
        targeted_events = 0

        def add_many(items: list[PolymarketMarket]) -> None:
            for m in items:
                by_id.setdefault(m.id, m)

        for page in range(max(1, int(self.cfg.polymarket_broad_pages))):
            try:
                items = self.fetch_active_markets(limit=broad_limit, offset=page * broad_limit)
                calls += 1
                broad_raw += len(items)
                filtered = [m for m in items if _looks_like_sports_market(m)]
                broad_kept += len(filtered)
                add_many(filtered)
            except Exception as exc:
                log.warning('polymarket broad fetch failed page=%s err=%s', page, exc)

        events_by_sport: dict[str, list[ExternalEvent]] = defaultdict(list)
        for event in events:
            events_by_sport[event.sport_key].append(event)

        for sport_key, sport_events in events_by_sport.items():
            for event in sport_events[: max(1, int(self.cfg.polymarket_target_events_per_sport))]:
                targeted_events += 1
                for query in _event_queries(event):
                    try:
                        items = self.fetch_active_markets(
                            limit=int(self.cfg.polymarket_search_results_per_query),
                            offset=0,
                            search=query,
                        )
                        calls += 1
                        targeted_raw += len(items)
                        filtered = [m for m in items if _market_relevant_to_event(event, m)]
                        targeted_kept += len(filtered)
                        add_many(filtered)
                    except Exception as exc:
                        log.debug('polymarket targeted fetch failed sport=%s query=%s err=%s', sport_key, query, exc)

        log.info(
            'polymarket_discovery markets=%s api_calls=%s targeted_events=%s broad_raw=%s broad_kept=%s targeted_raw=%s targeted_kept=%s',
            len(by_id), calls, targeted_events, broad_raw, broad_kept, targeted_raw, targeted_kept,
        )
        return list(by_id.values())

    def _parse_market(self, m: dict[str, Any]) -> PolymarketMarket:
        token_ids = [str(x) for x in _json_list(m.get('clobTokenIds'))]
        outcomes = [str(x) for x in _json_list(m.get('outcomes'))]
        prices_raw = _json_list(m.get('outcomePrices'))
        outcome_prices = []
        for p in prices_raw:
            fp = _safe_float(p)
            if fp is not None:
                outcome_prices.append(fp)

        best_bid = _safe_float(m.get('bestBid'))
        best_ask = _safe_float(m.get('bestAsk'))
        if (best_bid is None or best_ask is None) and outcome_prices:
            p = outcome_prices[0]
            if 0 < p < 1:
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
            yes_token_id=token_ids[0] if len(token_ids) > 0 else None,
            no_token_id=token_ids[1] if len(token_ids) > 1 else None,
            outcomes=outcomes,
            token_ids=token_ids,
            outcome_prices=outcome_prices,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            liquidity=float(_safe_float(m.get('liquidity') or m.get('liquidityNum'), 0.0) or 0.0),
            raw_payload=m,
        )
