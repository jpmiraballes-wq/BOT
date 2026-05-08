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


TEAM_ALIASES: dict[str, list[str]] = {
    # MLB
    'arizona diamondbacks': ['diamondbacks', 'dbacks', 'ari'],
    'atlanta braves': ['braves', 'atl'],
    'baltimore orioles': ['orioles', 'o s', 'bal'],
    'boston red sox': ['red sox', 'bos'],
    'chicago cubs': ['cubs', 'chc'],
    'chicago white sox': ['white sox', 'cws'],
    'cincinnati reds': ['reds', 'cin'],
    'cleveland guardians': ['guardians', 'cle'],
    'colorado rockies': ['rockies', 'col'],
    'detroit tigers': ['tigers', 'det'],
    'houston astros': ['astros', 'hou'],
    'kansas city royals': ['royals', 'kc'],
    'los angeles angels': ['angels', 'la angels', 'anaheim angels', 'laa'],
    'los angeles dodgers': ['dodgers', 'lad'],
    'miami marlins': ['marlins', 'mia'],
    'milwaukee brewers': ['brewers', 'mil'],
    'minnesota twins': ['twins', 'min'],
    'new york mets': ['mets', 'nym'],
    'new york yankees': ['yankees', 'nyy'],
    'oakland athletics': ['athletics', 'a s', 'oakland a s', 'oak'],
    'philadelphia phillies': ['phillies', 'phi'],
    'pittsburgh pirates': ['pirates', 'pit'],
    'san diego padres': ['padres', 'sd'],
    'san francisco giants': ['giants', 'sf'],
    'seattle mariners': ['mariners', 'sea'],
    'st louis cardinals': ['cardinals', 'cards', 'stl'],
    'tampa bay rays': ['rays', 'tb'],
    'texas rangers': ['rangers', 'tex'],
    'toronto blue jays': ['blue jays', 'jays', 'tor'],
    'washington nationals': ['nationals', 'nats', 'wsh'],

    # NBA
    'atlanta hawks': ['hawks', 'atl'],
    'boston celtics': ['celtics', 'bos'],
    'brooklyn nets': ['nets', 'bkn'],
    'charlotte hornets': ['hornets', 'cha'],
    'chicago bulls': ['bulls', 'chi'],
    'cleveland cavaliers': ['cavaliers', 'cavs', 'cle'],
    'dallas mavericks': ['mavericks', 'mavs', 'dal'],
    'denver nuggets': ['nuggets', 'den'],
    'detroit pistons': ['pistons', 'det'],
    'golden state warriors': ['warriors', 'gsw'],
    'houston rockets': ['rockets', 'hou'],
    'indiana pacers': ['pacers', 'ind'],
    'los angeles clippers': ['clippers', 'lac'],
    'los angeles lakers': ['lakers', 'lal'],
    'memphis grizzlies': ['grizzlies', 'mem'],
    'miami heat': ['heat', 'mia'],
    'milwaukee bucks': ['bucks', 'mil'],
    'minnesota timberwolves': ['timberwolves', 'wolves', 'min'],
    'new orleans pelicans': ['pelicans', 'pels', 'nop'],
    'new york knicks': ['knicks', 'nyk'],
    'oklahoma city thunder': ['thunder', 'okc'],
    'orlando magic': ['magic', 'orl'],
    'philadelphia 76ers': ['76ers', 'sixers', 'phi'],
    'phoenix suns': ['suns', 'phx'],
    'portland trail blazers': ['trail blazers', 'blazers', 'por'],
    'sacramento kings': ['kings', 'sac'],
    'san antonio spurs': ['spurs', 'sas'],
    'toronto raptors': ['raptors', 'tor'],
    'utah jazz': ['jazz', 'uta'],
    'washington wizards': ['wizards', 'was'],

    # NHL
    'anaheim ducks': ['ducks', 'ana'],
    'boston bruins': ['bruins', 'bos'],
    'buffalo sabres': ['sabres', 'buf'],
    'calgary flames': ['flames', 'cgy'],
    'carolina hurricanes': ['hurricanes', 'canes', 'car'],
    'chicago blackhawks': ['blackhawks', 'hawks', 'chi'],
    'colorado avalanche': ['avalanche', 'avs', 'col'],
    'columbus blue jackets': ['blue jackets', 'cbj'],
    'dallas stars': ['stars', 'dal'],
    'detroit red wings': ['red wings', 'det'],
    'edmonton oilers': ['oilers', 'edm'],
    'florida panthers': ['panthers', 'fla'],
    'los angeles kings': ['kings', 'lak'],
    'minnesota wild': ['wild', 'min'],
    'montreal canadiens': ['canadiens', 'habs', 'mtl'],
    'nashville predators': ['predators', 'preds', 'nsh'],
    'new jersey devils': ['devils', 'njd'],
    'new york islanders': ['islanders', 'nyi'],
    'new york rangers': ['rangers', 'nyr'],
    'ottawa senators': ['senators', 'sens', 'ott'],
    'philadelphia flyers': ['flyers', 'phi'],
    'pittsburgh penguins': ['penguins', 'pens', 'pit'],
    'san jose sharks': ['sharks', 'sj'],
    'seattle kraken': ['kraken', 'sea'],
    'st louis blues': ['blues', 'stl'],
    'tampa bay lightning': ['lightning', 'bolts', 'tbl'],
    'toronto maple leafs': ['maple leafs', 'leafs', 'tor'],
    'vancouver canucks': ['canucks', 'van'],
    'vegas golden knights': ['golden knights', 'knights', 'vgk'],
    'washington capitals': ['capitals', 'caps', 'wsh'],
    'winnipeg jets': ['jets', 'wpg'],
}

SPORT_DISCOVERY_QUERIES: dict[str, list[str]] = {
    'baseball_mlb': ['MLB', 'baseball', 'major league baseball'],
    'basketball_nba': ['NBA', 'basketball', 'NBA playoffs'],
    'icehockey_nhl': ['NHL', 'hockey', 'Stanley Cup'],
    'soccer_epl': ['Premier League', 'EPL', 'English Premier League', 'soccer'],
    'soccer_spain_la_liga': ['La Liga', 'Spanish La Liga', 'Barcelona Real Madrid', 'soccer'],
}


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
    s = s.replace('&', ' and ')
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _last_name(name: str) -> str:
    parts = [p for p in _normalize(name).split() if p]
    return parts[-1] if parts else ''


def _team_tokens(name: str) -> list[str]:
    text = _normalize(name)
    stop = {'fc', 'cf', 'club', 'the', 'de', 'la', 'los', 'las', 'real'}
    return [p for p in text.split() if len(p) >= 3 and p not in stop]


def _aliases_for_team(name: str) -> list[str]:
    norm = _normalize(name)
    aliases = [norm]
    aliases.extend(TEAM_ALIASES.get(norm, []))
    last = _last_name(norm)
    if last:
        aliases.append(last)
    aliases.extend(_team_tokens(norm)[:2])
    seen = set()
    out = []
    for alias in aliases:
        key = _normalize(alias)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _event_queries(event: ExternalEvent) -> list[str]:
    home = event.home_team or ''
    away = event.away_team or ''
    home_last = _last_name(home)
    away_last = _last_name(away)
    home_aliases = _aliases_for_team(home)
    away_aliases = _aliases_for_team(away)
    queries = []
    if home and away:
        queries.extend([
            f'{home} {away}',
            f'{away} {home}',
            f'{home} vs {away}',
            f'{away} vs {home}',
            f'{home} at {away}',
            f'{away} at {home}',
            f'{home} @ {away}',
            f'{away} @ {home}',
            f'{home_last} {away_last}'.strip(),
            f'{away_last} {home_last}'.strip(),
        ])
    for h in home_aliases[:4]:
        for a in away_aliases[:4]:
            if h != a:
                queries.extend([f'{h} {a}', f'{a} {h}', f'{h} vs {a}', f'{a} vs {h}'])
    for token in home_aliases[:4] + away_aliases[:4]:
        queries.append(token)
    queries.extend(SPORT_DISCOVERY_QUERIES.get(event.sport_key, [])[:2])
    seen = set()
    out = []
    for q in queries:
        q = ' '.join(str(q).split())
        key = q.lower()
        if key and key not in seen:
            seen.add(key)
            out.append(q)
    return out[:18]


def _contains_aliases(name: str, text: str) -> bool:
    if not name:
        return False
    wrapped = f' {text} '
    aliases = _aliases_for_team(name)
    for alias in aliases:
        if len(alias) >= 3 and f' {alias} ' in wrapped:
            return True
    norm = _normalize(name)
    if not norm:
        return False
    return max(fuzz.partial_ratio(norm, text), fuzz.token_set_ratio(norm, text)) >= 86


def _market_relevant_to_event(event: ExternalEvent, market: PolymarketMarket) -> bool:
    text = _normalize(' '.join([market.question, market.slug, market.category, ' '.join(market.outcomes or [])]))
    return _contains_aliases(event.home_team, text) and _contains_aliases(event.away_team, text)


def _looks_like_sports_market(market: PolymarketMarket) -> bool:
    text = _normalize(' '.join([market.question, market.slug, market.category, ' '.join(market.outcomes or [])]))
    sports_terms = [
        'ufc', 'mma', 'fight', 'boxing', 'soccer', 'football', 'premier',
        'champions league', 'la liga', 'serie a', 'nba', 'nfl', 'mlb', 'nhl',
        'tennis', 'atp', 'wta', 'world cup', 'baseball', 'basketball', 'hockey',
        'playoffs', 'stanley cup', 'reds', 'astros', 'yankees', 'dodgers', 'mets',
        'cubs', 'padres', 'phillies', 'braves', 'rockies', 'orioles', 'athletics',
        'blue jays', 'angels', 'red sox', 'rays', 'marlins', 'nationals',
        'guardians', 'twins', 'white sox', 'mariners', 'lakers', 'celtics',
        'knicks', 'warriors', 'thunder', 'nuggets', 'pacers', 'bruins', 'oilers',
        'panthers', 'rangers', 'maple leafs', 'real madrid', 'barcelona',
        'arsenal', 'chelsea', 'liverpool', 'manchester', 'tottenham',
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
        league_raw = 0
        league_kept = 0
        targeted_events = 0
        per_sport_hits: dict[str, dict[str, int]] = defaultdict(lambda: {'targeted_raw': 0, 'targeted_kept': 0, 'league_raw': 0, 'league_kept': 0})

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

        # League-level queries are cheap and catch markets where Gamma search fails on team pairs.
        for sport_key in events_by_sport.keys():
            for query in SPORT_DISCOVERY_QUERIES.get(sport_key, [])[:4]:
                try:
                    items = self.fetch_active_markets(
                        limit=int(self.cfg.polymarket_search_results_per_query),
                        offset=0,
                        search=query,
                    )
                    calls += 1
                    league_raw += len(items)
                    per_sport_hits[sport_key]['league_raw'] += len(items)
                    filtered = [m for m in items if _looks_like_sports_market(m)]
                    league_kept += len(filtered)
                    per_sport_hits[sport_key]['league_kept'] += len(filtered)
                    add_many(filtered)
                except Exception as exc:
                    log.debug('polymarket league fetch failed sport=%s query=%s err=%s', sport_key, query, exc)

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
                        per_sport_hits[sport_key]['targeted_raw'] += len(items)
                        filtered = [m for m in items if _market_relevant_to_event(event, m)]
                        targeted_kept += len(filtered)
                        per_sport_hits[sport_key]['targeted_kept'] += len(filtered)
                        add_many(filtered)
                    except Exception as exc:
                        log.debug('polymarket targeted fetch failed sport=%s query=%s err=%s', sport_key, query, exc)

        log.info(
            'polymarket_discovery markets=%s api_calls=%s targeted_events=%s broad_raw=%s broad_kept=%s league_raw=%s league_kept=%s targeted_raw=%s targeted_kept=%s by_sport=%s',
            len(by_id), calls, targeted_events, broad_raw, broad_kept, league_raw, league_kept,
            targeted_raw, targeted_kept, dict(per_sport_hits),
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
