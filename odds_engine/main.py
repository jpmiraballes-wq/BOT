from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace

from config import settings
from odds_client import OddsApiClient
from polymarket_client import PolymarketPublicClient
from mapper import build_mapping_candidates, best_candidate_for_event, score_event_against_markets
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


def _emit_no_candidate_diagnostic(event, sport_markets, runtime) -> None:
    """When an event ends up with no mapping candidate, log the top-3 closest
    Polymarket markets and the concrete reason each one was rejected.
    Pure observability: no logic change, no trades created here."""
    if not sport_markets:
        payload = {
            'event_id': event.id,
            'sport_key': event.sport_key,
            'home_team': event.home_team,
            'away_team': event.away_team,
            'reason': 'no_polymarket_market_for_this_sport_at_all',
            'top_candidates': [],
        }
        _log_to_base44('info', 'no_candidate_diagnostic', payload)
        log.info('no_candidate_diagnostic event=%s sport=%s NO MARKETS in pool', event.id, event.sport_key)
        return
    top = score_event_against_markets(event, sport_markets, runtime, k=3)
    payload = {
        'event_id': event.id,
        'sport_key': event.sport_key,
        'home_team': event.home_team,
        'away_team': event.away_team,
        'pool_size': len(sport_markets),
        'top_candidates': top,
    }
    _log_to_base44('info', 'no_candidate_diagnostic', payload)
    if top:
        best = top[0]
        log.info(
            'no_candidate_diagnostic event=%s sport=%s pool=%s best_q=%r conf=%.3f label=%s reason=%s home=%.2f away=%.2f both=%s',
            event.id, event.sport_key, len(sport_markets),
            (best.get('question') or '')[:80],
            float(best.get('confidence', 0.0)),
            best.get('validator_label'),
            best.get('reject_reason'),
            float(best.get('home_score', 0.0)),
            float(best.get('away_score', 0.0)),
            bool(best.get('both_present')),
        )
    else:
        log.info('no_candidate_diagnostic event=%s sport=%s pool=%s no scored candidates', event.id, event.sport_key, len(sport_markets))


# ----- Money Hunter V1 helpers ----------------------------------------------
def _top_signal_dict(signal, event, market, mapping, runtime) -> dict:
    """Build the per-signal diagnostic dict with thresholds and gaps."""
    min_edge = float(getattr(runtime, 'min_edge', 0.0) or 0.0)
    min_liq = float(getattr(runtime, 'min_liquidity', 0.0) or 0.0)
    max_spread = float(getattr(runtime, 'max_spread', 0.0) or 0.0)
    edge_bruto = float(getattr(signal, 'edge_bruto', 0.0) or 0.0)
    return {
        'sport_key': getattr(event, 'sport_key', 'unknown'),
        'external_event_id': signal.external_event_id,
        'polymarket_market_id': signal.polymarket_market_id,
        'market_question': getattr(market, 'question', '') or '',
        'outcome': signal.outcome,
        'fair_value': float(signal.fair_value or 0.0),
        'polymarket_price': float(signal.polymarket_price or 0.0),
        'edge_bruto': edge_bruto,
        'spread': float(signal.spread or 0.0),
        'edge_neto': float(signal.edge_neto or 0.0),
        'min_edge_required': min_edge,
        'edge_gap': round(min_edge - edge_bruto, 6),
        'liquidity': float(signal.liquidity or 0.0),
        'min_liquidity_required': min_liq,
        'spread_limit': max_spread,
        'confidence': float(signal.confidence or 0.0),
        'risk_status': signal.risk_status,
        'reject_reason': signal.reject_reason or '',
        'mapping_status': getattr(mapping, 'status', '') or '',
        'token_present': bool(signal.token_id),
        'explanation': signal.explanation,
    }


def _emit_top_signal_diagnostic(signal, event, market, mapping, runtime) -> None:
    payload = _top_signal_dict(signal, event, market, mapping, runtime)
    _log_to_base44('info', 'top_signal_diagnostic', payload)
    log.info(
        'top_signal_diagnostic sport=%s outcome=%s fair=%.4f price=%.4f edge=%.4f min_edge=%.4f gap=%.4f reason=%s token=%s',
        payload['sport_key'], payload['outcome'], payload['fair_value'],
        payload['polymarket_price'], payload['edge_bruto'],
        payload['min_edge_required'], payload['edge_gap'],
        payload['reject_reason'] or 'approved',
        'yes' if payload['token_present'] else 'no',
    )


def _log_edge_below_threshold(signal, event, market, runtime) -> None:
    min_edge = float(getattr(runtime, 'min_edge', 0.0) or 0.0)
    edge_bruto = float(getattr(signal, 'edge_bruto', 0.0) or 0.0)
    log.info(
        'edge_below_threshold sport=%s event=%s outcome=%s question=%r '
        'fair=%.4f polymarket_price=%.4f edge=%.4f min_edge=%.4f gap=%.4f',
        getattr(event, 'sport_key', 'unknown'),
        signal.external_event_id, signal.outcome,
        getattr(market, 'question', '') or '',
        float(signal.fair_value or 0.0),
        float(signal.polymarket_price or 0.0),
        edge_bruto, min_edge, round(min_edge - edge_bruto, 6),
    )


def _aggregate_blocked_reasons(stats_by_sport: dict) -> dict:
    totals: dict[str, int] = {}
    for stats in stats_by_sport.values():
        for reason, n in (stats.get('blocked_reasons') or {}).items():
            totals[reason] = totals.get(reason, 0) + int(n)
    return totals


def _money_hunter_decision(stats_by_sport: dict, signals_count: int, approved_count: int,
                            paper_count: int, test_paper_count: int, blocked_global: dict) -> tuple[str, str]:
    """Return (money_hunter_status, next_action) â pure observation, no thresholds touched."""
    if paper_count > 0:
        return 'PAPER_TRADES_CREATED', 'evaluate_paper_pnl_before_live'
    if test_paper_count > 0:
        return 'ONLY_TEST_TRADES', 'disable_test_mode_and_wait_for_real_edge'

    edge_block = int(blocked_global.get('edge_below_threshold', 0))
    no_cand = int(blocked_global.get('no_candidate_after_validator', 0))
    miss_token = int(blocked_global.get('missing_matched_outcome_token', 0))
    spread_block = int(blocked_global.get('spread_too_wide', 0))
    liq_block = int(blocked_global.get('liquidity_too_low', 0))

    sports_no_match = sum(
        1 for s in stats_by_sport.values()
        if int(s.get('mapping_candidates', 0)) == 0 and int(s.get('events', 0)) > 0
    )

    if signals_count > 0 and approved_count == 0 and edge_block > 0:
        return 'PIPELINE_OK_NO_EDGE', 'watch_more_or_lower_threshold_in_paper_only'
    if miss_token > 0 and signals_count > 0:
        return 'TOKEN_MAPPING_GAP', 'fix_outcome_token_mapping'
    if spread_block > 0 or liq_block > 0:
        return 'MARKET_QUALITY_BLOCKED', 'wait_for_better_market_quality'
    if sports_no_match >= 2 or no_cand > 0:
        return 'NEEDS_DISCOVERY_EXPANSION', 'improve_polymarket_discovery_for_sport'
    if int(sum(s.get('mapping_candidates', 0) for s in stats_by_sport.values())) == 0:
        return 'NO_MARKET_MATCHES', 'improve_polymarket_discovery_for_sport'
    return 'OBSERVING', 'keep_watching_no_action_needed'


def _build_money_hunter_report(runtime, stats_by_sport: dict, events, odds_outcomes,
                                markets, mappings, signals_count, approved_count,
                                paper_count, test_paper_count, top_signal_diagnostics,
                                money_hunter_status, next_action) -> dict:
    blocked_global = _aggregate_blocked_reasons(stats_by_sport)
    by_sport = []
    for sport_key, stats in stats_by_sport.items():
        blocked = stats.get('blocked_reasons') or {}
        top_reason = max(blocked.items(), key=lambda kv: kv[1])[0] if blocked else ''
        by_sport.append({
            'sport_key': sport_key,
            'events': int(stats.get('events', 0)),
            'odds_outcomes': int(stats.get('odds_outcomes', 0)),
            'polymarket_markets': int(stats.get('polymarket_markets', 0)),
            'mapping_candidates': int(stats.get('mapping_candidates', 0)),
            'auto_approved_mappings': int(stats.get('auto_approved_mappings', 0)),
            'signals': int(stats.get('signals', 0)),
            'positive_net_edge_signals': int(stats.get('positive_net_edge_signals', 0)),
            'approved_signals': int(stats.get('approved_signals', 0)),
            'paper_trades_created': int(stats.get('paper_trades_created', 0)),
            'top_blocked_reason': top_reason,
            'blocked_reasons': dict(blocked),
        })
    # Keep only the most informative diagnostics to avoid bloating BotLog.
    top_diags = top_signal_diagnostics[:10]
    return {
        'mode': runtime.bot_mode,
        'paper_force_test_trade': bool(getattr(runtime, 'paper_force_test_trade', False)),
        'total_events': len(events),
        'total_odds_outcomes': len(odds_outcomes),
        'total_polymarket_markets': len(markets),
        'total_mapping_candidates': len(mappings),
        'total_signals': int(signals_count),
        'approved_signals': int(approved_count),
        'real_paper_trades': int(paper_count),
        'test_paper_trades': int(test_paper_count),
        'blocked_reasons': blocked_global,
        'by_sport': by_sport,
        'top_signal_diagnostics_sample': top_diags,
        'money_hunter_status': money_hunter_status,
        'next_action': next_action,
    }
# ----- end Money Hunter V1 helpers ------------------------------------------

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
    markets_by_sport: dict[str, list] = {}
    for ev in events:
        bucket = markets_by_sport.setdefault(ev.sport_key, [])
    # Heuristic: assume all discovered markets are eligible to be scored against
    # any event in the same sport. We don't filter by sport here because the
    # discovery already filters with _looks_like_sports_market / _market_relevant_to_event.
    _all_markets = list(markets)
    for sport_key in list(markets_by_sport.keys()):
        markets_by_sport[sport_key] = _all_markets

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
    test_paper_count = 0
    top_signal_diagnostics: list[dict] = []
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
            try:
                _emit_no_candidate_diagnostic(event, markets_by_sport.get(event.sport_key, []), runtime)
            except Exception as exc:
                log.debug('no_candidate_diagnostic failed event=%s err=%s', event.id, exc)
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
            _emit_top_signal_diagnostic(signal, event, market, mapping, runtime)
            top_signal_diagnostics.append(_top_signal_dict(signal, event, market, mapping, runtime))
            if signal.reject_reason == 'edge_below_threshold':
                _log_edge_below_threshold(signal, event, market, runtime)
            trade = paper.open_from_signal(signal)
            if trade:
                papertrade_path = settings.data_dir / 'papertrade.jsonl'
                already_open = False
                if papertrade_path.exists():
                    for line in papertrade_path.read_text(errors='ignore').splitlines():
                        if f'"id": "{trade.id}"' in line and '"status": "open"' in line:
                            already_open = True
                            break

                if already_open:
                    log.info('paper_trade_duplicate_skipped id=%s signal=%s market=%s',
                             trade.id, trade.signal_id, trade.polymarket_market_id)
                else:
                    _write('PaperTrade', trade)
                    paper_count += 1
                    sport_stats['paper_trades_created'] += 1
            elif test_paper_count == 0 and runtime.paper_force_test_trade:
                test_trade = paper.force_test_trade_from_signal(signal)
                if test_trade:
                    _write('PaperTrade', test_trade)
                    test_paper_count += 1
                    sport_stats['paper_trades_created'] = sport_stats.get('paper_trades_created', 0) + 1
                    _log_to_base44('info', 'paper_trade_test_created', {
                        'event': signal.external_event_id,
                        'market': signal.polymarket_market_id,
                        'edge_neto': signal.edge_neto,
                        'reason': 'forced_paper_pipeline_test',
                    })
                    log.info('paper_trade_test_created event=%s market=%s edge_neto=%s',
                             signal.external_event_id, signal.polymarket_market_id, signal.edge_neto)

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
        'test_paper_trades': test_paper_count,
        'paper_force_test_trade': runtime.paper_force_test_trade,
        'base44_write_enabled': settings.base44_write_enabled,
    }
    blocked_global = _aggregate_blocked_reasons(stats_by_sport)
    money_hunter_status, next_action = _money_hunter_decision(
        stats_by_sport, signals_count, approved_count,
        paper_count, test_paper_count, blocked_global,
    )
    summary['money_hunter_status'] = money_hunter_status
    summary['next_action'] = next_action
    money_hunter_report = _build_money_hunter_report(
        runtime, stats_by_sport, events, odds_outcomes, markets, mappings,
        signals_count, approved_count, paper_count, test_paper_count,
        top_signal_diagnostics, money_hunter_status, next_action,
    )
    _log_to_base44('info', 'money_hunter_report', money_hunter_report)
    log.info('money_hunter_status=%s next_action=%s', money_hunter_status, next_action)
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
