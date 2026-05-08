from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace

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
from models import BotLog, now_iso, stable_id
from multi_sport_probe import controlled_sport_keys, new_stats, bump_blocked, probe_payload

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('odds_engine')


def _index_markets(markets):
    return {m.id: m for m in markets}


def _outcomes_by_event(outcomes):
    out = {}
    for o in outcomes:
        out.setdefault(o.external_event_id, []).append(o)
    return out


def _dedupe_key(entity: str, item) -> str:
    payload = item.base44_payload() if hasattr(item, 'base44_payload') else item
    if entity == 'ExternalEvent':
        return f"{entity}:{payload.get('external_id')}"
    if entity == 'PolymarketEvent':
        return f"{entity}:{payload.get('polymarket_id') or payload.get('slug')}"
    if entity == 'PolymarketMarket':
        return f"{entity}:{payload.get('condition_id') or payload.get('question')}"
    if entity == 'EventMapping':
        return f"{entity}:{payload.get('external_event_id')}:{payload.get('polymarket_event_id')}"
    if entity == 'MarketMapping':
        return f"{entity}:{payload.get('event_mapping_id')}:{payload.get('polymarket_market_id')}:{payload.get('external_outcome')}"
    if entity in {'OddsSnapshot', 'PolymarketSnapshot'}:
        captured = str(payload.get('captured_at') or now_iso())[:16]
        return f"{entity}:{stable_id(str(payload))}:{captured}"
    if entity == 'Signal':
        return f"{entity}:{payload.get('external_event_id')}:{payload.get('polymarket_market_id')}:{payload.get('outcome')}"
    if entity == 'PaperTrade':
        return f"{entity}:{payload.get('signal_id') or stable_id(str(payload))}"
    return f"{entity}:{stable_id(str(payload))}"


def _write(entity: str, item, send_base44: bool = True) -> bool:
    store.append(entity.lower(), item)
    if not send_base44 or not settings.base44_write_enabled:
        return False
    key = _dedupe_key(entity, item)
    if store.base44_was_sent(key):
        return False
    if hasattr(item, 'base44_payload'):
        result = base44.post_record(entity, item.base44_payload())
    else:
        result = base44.post_record(entity, item)
    if result is not None:
        store.mark_base44_sent(key)
        return True
    return False


def _log_to_base44(level: str, message: str, data: dict) -> None:
    item = BotLog(level=level, source='odds_engine', message=message, data=data, created_at=now_iso())
    store.append('bot_logs', item)
    if settings.base44_write_enabled:
        base44.post_record('BotLog', item.base44_payload())


def _emit_mapping_debug(events, markets_by_id, mappings, limit: int = 8) -> None:
    by_event = {}
    labels = Counter()
    statuses = Counter()
    for m in mappings:
        by_event.setdefault(m.external_event_id, []).append(m)
        statuses[m.status] += 1
        label = m.confidence_breakdown.get('validator_label') if isinstance(m.confidence_breakdown, dict) else None
        if label:
            labels[label] += 1

    debug_rows = []
    for event in events[:limit]:
        matches = by_event.get(event.id, [])
        if not matches:
            debug_rows.append({
                'event': f'{event.home_team} vs {event.away_team}',
                'sport_key': getattr(event, 'sport_key', None),
                'best_question': None,
                'reason': 'no_candidate_after_validator',
            })
            continue
        best = matches[0]
        market = markets_by_id.get(best.polymarket_market_id)
        br = best.confidence_breakdown or {}
        debug_rows.append({
            'event': f'{event.home_team} vs {event.away_team}',
            'sport_key': getattr(event, 'sport_key', None),
            'best_question': market.question if market else None,
            'confidence': best.confidence_score,
            'status': best.status,
            'market_type': best.market_type,
            'validator_label': br.get('validator_label'),
            'validator_reason': br.get('validator_reason'),
            'home_score': br.get('home_score'),
            'away_score': br.get('away_score'),
            'both_present': br.get('both_present'),
            'liquidity': market.liquidity if market else None,
            'spread': market.spread if market else None,
        })

    payload = {
        'mapping_candidates': len(mappings),
        'validator_labels': dict(labels),
        'mapping_statuses': dict(statuses),
        'top_events': debug_rows,
    }
    log.info('mapping_debug: %s', payload)
    _log_to_base44('info', 'mapping_debug', payload)


def _init_sport_stats(sport_keys: list[str]) -> dict[str, dict]:
    return {sport_key: new_stats() for sport_key in sport_keys}


def _sport_stats_for(stats_by_sport: dict[str, dict], sport_key: str) -> dict:
    if sport_key not in stats_by_sport:
        stats_by_sport[sport_key] = new_stats()
    return stats_by_sport[sport_key]


def _emit_sport_probe_logs(stats_by_sport: dict[str, dict]) -> None:
    for sport_key, stats in stats_by_sport.items():
        payload = probe_payload(sport_key, stats)
        log.info('sport_probe: %s', payload)
        _log_to_base44('info', 'sport_probe', payload)


def run_once() -> dict:
    settings.validate()
    bot_cfg = base44.fetch_bot_config() if settings.base44_write_enabled else {}
    runtime = settings.with_bot_config(bot_cfg)
    # Controlled multi-sport PAPER probe. It only widens data collection/mapping.
    # RiskManager and PaperBroker still block live modes and only allow PAPER trades.
    runtime = replace(runtime, odds_sport_keys=controlled_sport_keys(runtime.odds_sport_keys))
    log.info(
        'starting odds_engine run_once mode=%s sports=%s base44_write=%s',
        runtime.bot_mode,
        runtime.odds_sport_keys,
        settings.base44_write_enabled,
    )

    odds_client = OddsApiClient(runtime)
    poly_client = PolymarketPublicClient(runtime)
    risk = RiskManager(runtime)
    paper = PaperBroker(risk, runtime)

    events, odds_outcomes = odds_client.fetch_events_with_odds()
    markets = poly_client.fetch_markets_for_events(events)
    fair_values = aggregate_fair_values(odds_outcomes)
    mappings = build_mapping_candidates(events, markets, runtime)
    markets_by_id = _index_markets(markets)
    outcomes_by_event = _outcomes_by_event(odds_outcomes)
    _emit_mapping_debug(events, markets_by_id, mappings)

    event_sport = {event.id: event.sport_key for event in events}
    stats_by_sport = _init_sport_stats(runtime.odds_sport_keys)
    for event in events:
        _sport_stats_for(stats_by_sport, event.sport_key)['events'] += 1
    for outcome in odds_outcomes:
        sport_key = event_sport.get(outcome.external_event_id, 'unknown')
        _sport_stats_for(stats_by_sport, sport_key)['odds_outcomes'] += 1
    mapped_market_ids_by_sport: dict[str, set[str]] = {k: set() for k in stats_by_sport}
    for mapping in mappings:
        sport_key = event_sport.get(mapping.external_event_id, 'unknown')
        stats = _sport_stats_for(stats_by_sport, sport_key)
        stats['mapping_candidates'] += 1
        mapped_market_ids_by_sport.setdefault(sport_key, set()).add(mapping.polymarket_market_id)
        if mapping.status == 'auto_approved':
            stats['auto_approved_mappings'] += 1
        else:
            bump_blocked(stats, 'mapping_not_auto_approved')
    for sport_key, market_ids in mapped_market_ids_by_sport.items():
        _sport_stats_for(stats_by_sport, sport_key)['polymarket_markets'] = len(market_ids)

    for idx, e in enumerate(events):
        _write('ExternalEvent', e, send_base44=idx < runtime.base44_max_events)
    for idx, o in enumerate(odds_outcomes):
        _write('OddsSnapshot', o, send_base44=idx < runtime.base44_max_odds_snapshots)
    for idx, m in enumerate(markets):
        send = idx < runtime.base44_max_polymarket_markets
        _write('PolymarketEvent', m.base44_event_payload(), send_base44=send)
        _write('PolymarketMarket', m.base44_market_payload(), send_base44=send)
        _write('PolymarketSnapshot', m.base44_snapshot_payload(), send_base44=send)
    for idx, c in enumerate(mappings):
        send = idx < runtime.base44_max_mappings
        _write('EventMapping', c.base44_event_mapping_payload(), send_base44=send)
        _write('MarketMapping', c.base44_market_mapping_payload(), send_base44=send)

    signals_count = 0
    approved_count = 0
    paper_count = 0
    seen_signals_this_run: set[str] = set()

    enabled = bool(bot_cfg.get('enabled', False)) if bot_cfg else False
    if not enabled:
        _emit_sport_probe_logs(stats_by_sport)
        summary = {
            'mode': runtime.bot_mode,
            'enabled': False,
            'sports': runtime.odds_sport_keys,
            'events': len(events),
            'odds_outcomes': len(odds_outcomes),
            'polymarket_markets': len(markets),
            'mapping_candidates': len(mappings),
            'signals': 0,
            'approved_signals': 0,
            'paper_trades': 0,
            'note': 'BotConfig.enabled=false or Base44 writes disabled, ingestion only',
            'base44_write_enabled': settings.base44_write_enabled,
        }
        _log_to_base44('info', 'run_once_ingestion_only', summary)
        log.info('summary: %s', summary)
        return summary

    for event in events:
        sport_stats = _sport_stats_for(stats_by_sport, event.sport_key)
        mapping = best_candidate_for_event(event.id, mappings)
        if not mapping:
            bump_blocked(sport_stats, 'no_candidate_after_validator')
            continue
        market = markets_by_id.get(mapping.polymarket_market_id)
        if not market:
            bump_blocked(sport_stats, 'missing_polymarket_market')
            continue
        for odds in outcomes_by_event.get(event.id, []):
            fair = event_fair_value_for_name(event.id, odds.outcome_name, fair_values)
            if fair is None:
                bump_blocked(sport_stats, 'missing_fair_value')
                continue
            if odds.outcome_name.lower() not in (market.question + ' ' + market.slug).lower():
                bump_blocked(sport_stats, 'outcome_not_in_polymarket_question')
                continue
            signal_key = f"{event.id}:{market.id}:{market.yes_token_id or ''}"
            if signal_key in seen_signals_this_run:
                continue
            seen_signals_this_run.add(signal_key)
            signal = build_buy_signal(mapping, market, odds, fair, risk, runtime)
            _write('Signal', signal)
            signals_count += 1
            sport_stats['signals'] += 1
            if signal.edge_neto > 0:
                sport_stats['positive_net_edge_signals'] += 1
            if signal.risk_status == 'approved':
                approved_count += 1
                sport_stats['approved_signals'] += 1
            else:
                bump_blocked(sport_stats, signal.reject_reason or 'rejected')
            trade = paper.open_from_signal(signal)
            if trade:
                _write('PaperTrade', trade)
                paper_count += 1
                sport_stats['paper_trades_created'] += 1

    _emit_sport_probe_logs(stats_by_sport)
    summary = {
        'mode': runtime.bot_mode,
        'enabled': enabled,
        'sports': runtime.odds_sport_keys,
        'events': len(events),
        'odds_outcomes': len(odds_outcomes),
        'polymarket_markets': len(markets),
        'mapping_candidates': len(mappings),
        'signals': signals_count,
        'approved_signals': approved_count,
        'paper_trades': paper_count,
        'base44_write_enabled': settings.base44_write_enabled,
    }
    _log_to_base44('info', 'run_once_complete', summary)
    log.info('summary: %s', summary)
    return summary


def main() -> int:
    """Run exactly one scan.

    launchd already schedules this job with StartInterval=60. Keeping an
    internal while/sleep loop here creates overlapping engines and stale runs.
    """
    try:
        run_once()
        return 0
    except Exception as exc:
        log.exception('run failed: %s', exc)
        _log_to_base44('error', 'run_failed', {'error': str(exc)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
