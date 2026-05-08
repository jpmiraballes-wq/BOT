from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import requests

from config import settings
from models import ExternalEvent, OddsOutcome, stable_id, now_iso


def _decimal_from_american(price: float) -> float:
    if price > 0:
        return 1.0 + price / 100.0
    return 1.0 + 100.0 / abs(price)


def _to_decimal(price: Any) -> float:
    value = float(price)
    if value <= 0:
        raise ValueError('odds price must be positive decimal or American')
    if value >= 100:
        return _decimal_from_american(value)
    return value


def _normalize_name(name: str) -> str:
    import unicodedata, re
    s = unicodedata.normalize('NFKD', name or '').encode('ascii', 'ignore').decode('ascii')
    s = s.lower()
    s = re.sub(r'[^a-z0-9 ]+', ' ', s)
    s = re.sub(r'\b(fc|cf|club|the)\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


class OddsApiClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.odds_api_key
        self.base_url = 'https://api.the-odds-api.com/v4'

    def fetch_events_with_odds(self) -> tuple[list[ExternalEvent], list[OddsOutcome]]:
        events: list[ExternalEvent] = []
        outcomes: list[OddsOutcome] = []
        for sport_key in settings.odds_sport_keys:
            evs, outs = self._fetch_sport(sport_key)
            events.extend(evs)
            outcomes.extend(outs)
        return events, outcomes

    def _fetch_sport(self, sport_key: str) -> tuple[list[ExternalEvent], list[OddsOutcome]]:
        url = f'{self.base_url}/sports/{sport_key}/odds'
        params = {
            'apiKey': self.api_key,
            'regions': settings.odds_regions,
            'markets': settings.odds_markets,
            'oddsFormat': 'decimal',
            'dateFormat': 'iso',
        }
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        received_at = now_iso()
        events: list[ExternalEvent] = []
        outcomes: list[OddsOutcome] = []
        for item in data:
            home = item.get('home_team') or ''
            away = item.get('away_team') or ''
            event_id = stable_id('oddsapi', item.get('id'), sport_key, home, away, item.get('commence_time'))
            event = ExternalEvent(
                id=event_id,
                provider='the_odds_api',
                provider_event_id=str(item.get('id') or event_id),
                sport_key=sport_key,
                league=str(item.get('sport_title') or sport_key),
                home_team=home,
                away_team=away,
                commence_time_utc=str(item.get('commence_time') or ''),
                participants_normalized=[_normalize_name(home), _normalize_name(away)],
                raw_payload=item,
            )
            events.append(event)
            outcomes.extend(self._extract_h2h_outcomes(event, item, received_at))
        return events, outcomes

    def _extract_h2h_outcomes(self, event: ExternalEvent, item: dict, received_at: str) -> list[OddsOutcome]:
        out: list[OddsOutcome] = []
        for bookmaker in item.get('bookmakers') or []:
            bookmaker_key = str(bookmaker.get('key') or bookmaker.get('title') or 'unknown')
            last_update = bookmaker.get('last_update')
            for market in bookmaker.get('markets') or []:
                market_key = str(market.get('key') or '')
                if market_key != 'h2h':
                    continue
                raw_probs = []
                rows = []
                for outcome in market.get('outcomes') or []:
                    try:
                        dec = _to_decimal(outcome.get('price'))
                        prob = 1.0 / dec
                    except Exception:
                        continue
                    raw_probs.append(prob)
                    rows.append((outcome, dec, prob))
                total = sum(raw_probs) or 1.0
                for outcome, dec, prob in rows:
                    out.append(OddsOutcome(
                        external_event_id=event.id,
                        bookmaker=bookmaker_key,
                        market_key=market_key,
                        outcome_name=str(outcome.get('name') or ''),
                        decimal_odds=dec,
                        implied_probability_raw=prob,
                        implied_probability_normalized=prob / total,
                        provider_last_update=str(last_update or market.get('last_update') or received_at),
                        received_at=received_at,
                        raw_payload={'bookmaker': bookmaker, 'market': market, 'outcome': outcome},
                    ))
        return out
