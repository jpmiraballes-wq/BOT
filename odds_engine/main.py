from __future__ import annotations

import logging
import time

from config import settings
from odds_client import OddsApiClient
from polymarket_client import PolymarketPublicClient
from mapper import build_mapping_candidates, best_candidate_for_event
from fair_value import aggregate_fair_values, event_fair_value_for_name
from risk_manager import RiskManager
from signal_engine import build_buy_signal
from paper_broker import PaperBroker
from storage import store
from base44_client import base44

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('odds_engine')


def _index_markets(markets):
    return {m.id: m for m in markets}


def _outcomes_by_event(outcomes):
    out = {}
    for o in outcomes:
        out.setdefault(o.external_event_id, []).append(o)
    return out


def run_once() -> dict:
    settings.validate()
    log.info('starting odds_engine run_once mode=%s sports=%s', settings.bot_mode, settings.odds_sport_keys)

    odds_client = OddsApiClient()
    poly_client = PolymarketPublicClient()
    risk = RiskManager()
    paper = PaperBroker(risk)

    events, odds_outcomes = odds_client.fetch_events_with_odds()
    markets = poly_client.fetch_active_markets()
    fair_values = aggregate_fair_values(odds_outcomes)
    mappings = build_mapping_candidates(events, markets)
    markets_by_id = _index_markets(markets)
    outcomes_by_event = _outcomes_by_event(odds_outcomes)

    for e in events:
        store.append('external_events', e)
    for o in odds_outcomes:
        store.append('odds_snapshots', o)
    for m in markets:
        store.append('polymarket_markets', m)
    for c in mappings[:1000]:
        store.append('mapping_candidates', c)

    signals_count = 0
    approved_count = 0
    paper_count = 0

    for event in events:
        mapping = best_candidate_for_event(event.id, mappings)
        if not mapping:
            continue
        market = markets_by_id.get(mapping.polymarket_market_id)
        if not market:
            continue
        # V1 only tests home_team as YES target when title seems to include that participant.
        # If not found, it still logs rejected/needs_review via mapper status.
        for odds in outcomes_by_event.get(event.id, []):
            fair = event_fair_value_for_name(event.id, odds.outcome_name, fair_values)
            if fair is None:
                continue
            # Conservative: only build a BUY signal when the Polymarket title contains the outcome name.
            if odds.outcome_name.lower() not in (market.question + ' ' + market.slug).lower():
                continue
            signal = build_buy_signal(mapping, market, odds, fair, risk)
            store.append('signals', signal)
            base44.post_record('Signal', signal)
            signals_count += 1
            if signal.risk_status == 'approved':
                approved_count += 1
            trade = paper.open_from_signal(signal)
            if trade:
                store.append('paper_trades', trade)
                base44.post_record('PaperTrade', trade)
                paper_count += 1

    summary = {
        'mode': settings.bot_mode,
        'events': len(events),
        'odds_outcomes': len(odds_outcomes),
        'polymarket_markets': len(markets),
        'mapping_candidates': len(mappings),
        'signals': signals_count,
        'approved_signals': approved_count,
        'paper_trades': paper_count,
    }
    store.log('info', 'main', 'run_once_complete', summary)
    log.info('summary: %s', summary)
    return summary


def main() -> int:
    while True:
        try:
            run_once()
        except Exception as exc:
            log.exception('run failed: %s', exc)
            store.log('error', 'main', 'run_failed', {'error': str(exc)})
        if settings.bot_mode == 'OBSERVE':
            # OBSERVE can loop safely. PAPER also loops, but this branch documents intent.
            pass
        time.sleep(settings.loop_interval_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
