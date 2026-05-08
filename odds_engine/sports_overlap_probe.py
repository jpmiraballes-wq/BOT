from __future__ import annotations

import logging
from dataclasses import replace
from collections import Counter

from config import settings
from odds_client import OddsApiClient
from polymarket_client import PolymarketPublicClient
from mapper import build_mapping_candidates

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('sports_overlap_probe')

PREFERRED_SPORTS = [
    'mma_mixed_martial_arts',
    'soccer_uefa_champs_league',
    'soccer_epl',
    'soccer_spain_la_liga',
    'soccer_italy_serie_a',
    'soccer_germany_bundesliga',
    'basketball_nba',
    'americanfootball_nfl',
    'icehockey_nhl',
    'baseball_mlb',
    'tennis_atp',
    'tennis_wta',
]


def _available_sports(client: OddsApiClient) -> set[str]:
    try:
        sports = client.fetch_sports()
        return {str(s.get('key')) for s in sports if s.get('key') and s.get('active', True)}
    except Exception as exc:
        log.warning('sports_list_failed: %s', exc)
        return set(PREFERRED_SPORTS)


def _examples(events, markets_by_id, mappings, limit=3):
    out = []
    by_event = {}
    for m in mappings:
        by_event.setdefault(m.external_event_id, []).append(m)
    for event in events:
        matches = by_event.get(event.id) or []
        if not matches:
            continue
        best = matches[0]
        market = markets_by_id.get(best.polymarket_market_id)
        br = best.confidence_breakdown or {}
        out.append({
            'event': f'{event.home_team} vs {event.away_team}',
            'question': market.question if market else None,
            'confidence': best.confidence_score,
            'status': best.status,
            'validator_label': br.get('validator_label'),
            'home_score': br.get('home_score'),
            'away_score': br.get('away_score'),
            'liquidity': market.liquidity if market else None,
            'spread': market.spread if market else None,
        })
        if len(out) >= limit:
            break
    return out


def probe() -> list[dict]:
    base_client = OddsApiClient(settings)
    available = _available_sports(base_client)
    sports = [s for s in PREFERRED_SPORTS if s in available]
    results = []

    for sport_key in sports:
        cfg = replace(settings, odds_sport_keys=[sport_key], base44_write_enabled=False)
        odds = OddsApiClient(cfg)
        poly = PolymarketPublicClient(cfg)
        try:
            events, outcomes = odds.fetch_events_with_odds()
        except Exception as exc:
            results.append({'sport_key': sport_key, 'error': f'odds_failed: {exc}'})
            continue
        if not events:
            results.append({'sport_key': sport_key, 'events': 0, 'note': 'no_odds_events'})
            continue
        try:
            markets = poly.fetch_markets_for_events(events[:10])
        except Exception as exc:
            results.append({'sport_key': sport_key, 'events': len(events), 'error': f'polymarket_failed: {exc}'})
            continue

        mappings = build_mapping_candidates(events, markets, cfg)
        labels = Counter()
        statuses = Counter()
        for m in mappings:
            statuses[m.status] += 1
            br = m.confidence_breakdown or {}
            labels[br.get('validator_label') or 'unknown'] += 1
        markets_by_id = {m.id: m for m in markets}
        row = {
            'sport_key': sport_key,
            'events': len(events),
            'odds_outcomes': len(outcomes),
            'polymarket_markets': len(markets),
            'mapping_candidates': len(mappings),
            'validator_labels': dict(labels),
            'mapping_statuses': dict(statuses),
            'examples': _examples(events, markets_by_id, mappings),
        }
        results.append(row)
        log.info('overlap_probe_row: %s', row)

    ranked = sorted(results, key=lambda r: (r.get('mapping_statuses', {}).get('auto_approved', 0), r.get('mapping_candidates', 0)), reverse=True)
    log.info('overlap_probe_summary: %s', ranked)
    return ranked


if __name__ == '__main__':
    probe()
