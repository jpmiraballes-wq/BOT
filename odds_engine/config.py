from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')


def _csv(name: str, default: str = '') -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(',') if x.strip()]


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}


@dataclass(frozen=True)
class Settings:
    bot_mode: str = os.getenv('BOT_MODE', 'PAPER').upper()
    paper_force_test_trade: bool = _bool('PAPER_FORCE_TEST_TRADE', False)
    test_trade_size_usd: float = _float('TEST_TRADE_SIZE_USDC', 1.0)
    loop_interval_seconds: int = _int('LOOP_INTERVAL_SECONDS', 60)
    data_dir: Path = Path(os.getenv('DATA_DIR', './data'))

    odds_api_key: str = os.getenv('ODDS_API_KEY', '').strip()
    odds_regions: str = os.getenv('ODDS_REGIONS', 'us,eu')
    odds_markets: str = os.getenv('ODDS_MARKETS', 'h2h')
    odds_sport_keys: list[str] | None = None

    polymarket_gamma_url: str = os.getenv('POLYMARKET_GAMMA_URL', 'https://gamma-api.polymarket.com')
    polymarket_clob_url: str = os.getenv('POLYMARKET_CLOB_URL', 'https://clob.polymarket.com')
    polymarket_broad_limit: int = _int('POLYMARKET_BROAD_LIMIT', 300)
    polymarket_broad_pages: int = _int('POLYMARKET_BROAD_PAGES', 2)
    polymarket_target_events_per_sport: int = _int('POLYMARKET_TARGET_EVENTS_PER_SPORT', 8)
    polymarket_search_results_per_query: int = _int('POLYMARKET_SEARCH_RESULTS_PER_QUERY', 40)

    base44_base_url: str = os.getenv('BASE44_BASE_URL', os.getenv('BASE44_API_URL', 'https://app.base44.com')).strip()
    base44_api_key: str = os.getenv('EXTERNAL_BASE44_API_KEY', os.getenv('BASE44_API_KEY', os.getenv('BASE44_API_TOKEN', ''))).strip()
    base44_app_id: str = os.getenv('EXTERNAL_BASE44_APP_ID', os.getenv('BASE44_APP_ID', '69e1e225a40599eb44ced81e')).strip()

    # First-run throttle. Local JSONL still stores everything.
    base44_write_enabled: bool = _bool('BASE44_WRITE_ENABLED', True)
    base44_max_events: int = _int('BASE44_MAX_EVENTS', 20)
    base44_max_odds_snapshots: int = _int('BASE44_MAX_ODDS_SNAPSHOTS', 40)
    base44_max_polymarket_markets: int = _int('BASE44_MAX_POLYMARKET_MARKETS', 30)
    base44_max_mappings: int = _int('BASE44_MAX_MAPPINGS', 50)

    starting_capital_usd: float = _float('STARTING_CAPITAL_USD', 500.0)
    paper_trade_usd: float = _float('PAPER_TRADE_USD', 5.0)
    max_total_exposure_usd: float = _float('MAX_TOTAL_EXPOSURE_USD', 100.0)
    min_edge: float = _float('MIN_EDGE', 0.03)
    max_spread: float = _float('MAX_SPREAD', 0.04)
    min_liquidity: float = _float('MIN_LIQUIDITY', 1000.0)
    min_mapping_confidence: float = _float('MIN_MAPPING_CONFIDENCE', 0.85)
    default_odds_ttl_seconds: int = _int('DEFAULT_ODDS_TTL_SECONDS', 300)

    def __post_init__(self):
        # Important: dataclasses.replace(settings, odds_sport_keys=[...]) is used
        # by diagnostics/probes. Do not overwrite explicit sport keys with .env.
        if self.odds_sport_keys is None:
            object.__setattr__(self, 'odds_sport_keys', _csv('ODDS_SPORT_KEYS', 'mma_mixed_martial_arts'))
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.bot_mode not in {'OBSERVE', 'PAPER'}:
            raise RuntimeError('Only OBSERVE and PAPER are allowed in odds_engine V1')
        if not self.odds_api_key:
            raise RuntimeError('Missing ODDS_API_KEY in odds_engine/.env')

    def with_bot_config(self, config: dict) -> 'Settings':
        if not config:
            return self
        mode_raw = str(config.get('mode') or 'paper').upper()
        mode = 'PAPER' if mode_raw != 'OBSERVE' else 'OBSERVE'
        if mode_raw == 'LIVE':
            mode = 'PAPER'
        return replace(
            self,
            bot_mode=mode,
            min_edge=float(config.get('min_edge_pct') or self.min_edge),
            paper_trade_usd=float(config.get('max_position_size_usdc') or self.paper_trade_usd),
            max_total_exposure_usd=float(config.get('max_position_size_usdc') or self.paper_trade_usd) * float(config.get('max_open_positions') or 1),
        )


settings = Settings()
