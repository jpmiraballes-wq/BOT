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
from models import BotLog, now_iso

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('odds_engine')


def _index_markets(markets):
    return {m.id: m for m in markets}


def _outcomes_by_event(outcomes):
    out = {}
    for o in outcomes:
        out.setdefault(o.external_event_id, []).append(o)
    return out


def _write(entity: str, item) -> None:
    store.append(entity.lower(), item)
    if hasattr(item, 'base44_payload'):
        base44.post_record(entity, item.base44_payload())
    else:
        base44.post_record(entity, item)


def _log_to_base44(level: str, message: str, data: dict) -> None:
    item = BotLog(level=level, source='odds_engine', message=message, data=data, created_at=now_iso())
    store.append('bot_logs', item)
    base44.post_record('BotLog', item.base44_payload())


def run_once() -> dict:
    settings.validate()
    bot_cfg = base44.fetch_bot_config()
    runtime = settings.with_bot_config(bot_cfg)
    log.info('starting odds_engine run_once mode=%s sports=%s base44=%s', runtime.bot_mode, runtime.odds_sport_keys, base44.enabled)

    odds_client = OddsApiClient(runtime.odds_api_key)
    poly_client = PolymarketPublicClient()
    risk = RiskManager(runtime)
    paper = PaperBroker(risk, runtime)

    events, odds_outcomes = odds_client.fetch_events_with_odds()
    markets = poly_client.fetch_active_markets()
    fair_values = aggregate_fair_values(odds_outcomes)
    mappings = build_mapping_candidates(events, markets)
    markets_by_id = _index_markets(markets)
    outcomes_by_event = _outcomes_by_event(odds_outcomes)

    for e in events:
        _write('ExternalEvent', e)
    for o in odds_outcomes:
        _write('OddsSnapshot', o)
    for m in markets:
        _write('PolymarketEvent', m.base44_event_payload())
        _write('PolymarketMarket', m.base44_market_payload())
        _write('PolymarketSnapshot', m.base44_snapshot_payload())
    for c in mappings[:1000]:
        _write('EventMapping', c.base44_event_mapping_payload())
        _write('MarketMapping', c.base44_market_mapping_payload())

    signals_count = 0
    approved_count = 0
    paper_count = 0

    # If the dashboard switch is disabled, the engine still ingests reference data
    # but does not create Signals/PaperTrades. This is the safe default.
    enabled = bool(bot_cfg.get('enabled', False)) if bot_cfg else False
    if not enabled:
        summary = {
            'mode': runtime.bot_mode,
            'enabled': False,
            'events': len(events),
            'odds_outcomes': len(odds_outcomes),
            'polymarket_markets': len(markets),
            'mapping_candidates': len(mappings),
            'signals': 0,
            'approved_signals': 0,
            'paper_trades': 0,
            'note': 'BotConfig.enabled=false, ingestion only',
        }
        _log_to_base44('info', 'run_once_ingestion_only', summary)
        log.info('summary: %s', summary)
        return summary

    for event in events:
        mapping = best_candidate_for_event(event.id, mappings)
        if not mapping:
            continue
        market = markets_by_id.get(mapping.polymarket_market_id)
        if not market:
            continue
        for odds in outcomes_by_event.get(event.id, []):
            fair = event_fair_value_for_name(event.id, odds.outcome_name, fair_values)
            if fair is None:
                continue
            if odds.outcome_name.lower() not in (market.question + ' ' + market.slug).lower():
                continue
            signal = build_buy_signal(mapping, market, odds, fair, risk)
            _write('Signal', signal)
            signals_count += 1
            if signal.risk_status == 'approved':
                approved_count += 1
            trade = paper.open_from_signal(signal)
            if trade:
                _write('PaperTrade', trade)
                paper_count += 1

    summary = {
        'mode': runtime.bot_mode,
        'enabled': enabled,
        'events': len(events),
        'odds_outcomes': len(odds_outcomes),
        'polymarket_markets': len(markets),
        'mapping_candidates': len(mappings),
        'signals': signals_count,
        'approved_signals': approved_count,
        'paper_trades': paper_count,
    }
    _log_to_base44('info', 'run_once_complete', summary)
    log.info('summary: %s', summary)
    return summary


def main() -> int:
    while True:
        try:
            run_once()
        except Exception as exc:
            log.exception('run failed: %s', exc)
            _log_to_base44('error', 'run_failed', {'error': str(exc)})
        time.sleep(settings.loop_interval_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
