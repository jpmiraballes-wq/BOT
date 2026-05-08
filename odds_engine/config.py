from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Settings:
    bot_mode: str = os.getenv('BOT_MODE', 'PAPER').upper()
    loop_interval_seconds: int = _int('LOOP_INTERVAL_SECONDS', 60)
    data_dir: Path = Path(os.getenv('DATA_DIR', './data'))

    odds_api_key: str = os.getenv('ODDS_API_KEY', '').strip()
    odds_regions: str = os.getenv('ODDS_REGIONS', 'us,eu')
    odds_markets: str = os.getenv('ODDS_MARKETS', 'h2h')
    odds_sport_keys: list[str] = None  # set in __post_init__ style below

    polymarket_gamma_url: str = os.getenv('POLYMARKET_GAMMA_URL', 'https://gamma-api.polymarket.com')
    polymarket_clob_url: str = os.getenv('POLYMARKET_CLOB_URL', 'https://clob.polymarket.com')

    base44_api_url: str = os.getenv('BASE44_API_URL', '').strip()
    base44_api_token: str = os.getenv('BASE44_API_TOKEN', '').strip()
    base44_app_id: str = os.getenv('BASE44_APP_ID', '69e1e225a40599eb44ced81e')

    starting_capital_usd: float = _float('STARTING_CAPITAL_USD', 500.0)
    paper_trade_usd: float = _float('PAPER_TRADE_USD', 5.0)
    max_total_exposure_usd: float = _float('MAX_TOTAL_EXPOSURE_USD', 100.0)
    min_edge: float = _float('MIN_EDGE', 0.04)
    max_spread: float = _float('MAX_SPREAD', 0.04)
    min_liquidity: float = _float('MIN_LIQUIDITY', 1000.0)
    min_mapping_confidence: float = _float('MIN_MAPPING_CONFIDENCE', 0.85)
    default_odds_ttl_seconds: int = _int('DEFAULT_ODDS_TTL_SECONDS', 300)

    def __post_init__(self):
        object.__setattr__(self, 'odds_sport_keys', _csv('ODDS_SPORT_KEYS', 'mma_mixed_martial_arts'))
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.bot_mode not in {'OBSERVE', 'PAPER'}:
            raise RuntimeError('Only OBSERVE and PAPER are allowed in odds_engine V1')
        if not self.odds_api_key:
            raise RuntimeError('Missing ODDS_API_KEY in odds_engine/.env')


settings = Settings()
