"""stat_arb.py - Statistical arbitrage sobre pares correlacionados.

Estrategia:
  1) Universo: mercados activos con volumen >= MIN_VOLUME.
  2) Cada ciclo, actualiza historial de precios (YES price) en memoria.
  3) Cuando hay >= MIN_SAMPLES puntos, calcula correlacion Pearson entre
     todos los pares. Los que superan CORR_THRESHOLD se guardan como
     "pares activos".
  4) Para cada par activo, calcula el spread normalizado:
        spread_t = price_A_t - beta * price_B_t
     donde beta = cov(A,B)/var(B) (OLS simple).
     Z-score = (spread_t - mean(spread)) / std(spread).
  5) Entrada: |z| > Z_ENTRY. Si z > 0 -> A caro, B barato: vender A (short
     no existe en Polymarket, asi que compramos NO de A) + comprar YES de B.
     Simplificamos: solo tomamos el lado "barato" = comprar YES de B,
     esperando convergencia. Captura menos edge pero evita shorts.
  6) Salida: |z| < Z_EXIT o pasa MAX_HOLD_HOURS.
  7) Capital estricto de StrategyCapital['stat_arb'].

La correlacion se recalcula cada RECALC_INTERVAL_CYCLES ciclos para no
saturar CPU. El historial se persiste en /tmp/stat_arb_history.json para
sobrevivir restarts cortos.
"""

import json
import logging
import math
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from capital_allocator import CapitalAllocator
from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL, GAMMA_API_URL
from decision_logger import log_decision, log_warning

logger = logging.getLogger(__name__)

STRATEGY = "stat_arb"
HISTORY_PATH = "/tmp/stat_arb_history.json"
MAX_HISTORY_POINTS = 200
MIN_SAMPLES = 30
CORR_THRESHOLD = 0.75
Z_ENTRY = 2.0
Z_EXIT = 0.3
MAX_HOLD_HOURS = 24
MIN_VOLUME_USDC = 20_000.0
MIN_LIQUIDITY_USDC = 2_000.0
UNIVERSE_SIZE = 40  # top N por volumen
RECALC_PAIRS_EVERY = 10  # ciclos
POSITION_SIZE_USDC = 15.0  # tamaño fijo por entrada
MAX_CONCURRENT_POSITIONS = 3
REQUEST_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers estadisticos
# ---------------------------------------------------------------------------
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def _corr(xs, ys):
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _beta(xs, ys):
    # OLS: xs = alpha + beta*ys => beta = cov(x,y)/var(y)
    if len(xs) < 3:
        return 1.0
    mx, my = _mean(xs), _mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)
    vy = sum((y - my) ** 2 for y in ys) / (len(ys) - 1)
    if vy == 0:
        return 1.0
    return cov / vy


# ---------------------------------------------------------------------------
# Gamma fetch
# ---------------------------------------------------------------------------
def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_yes_price(market) -> Optional[float]:
    raw = market.get("outcomePrices")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        return float(raw[0])
    except (TypeError, ValueError):
        return None


def _extract_token_ids(market) -> List[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    return []


def _fetch_universe() -> List[Dict[str, Any]]:
    url = "%s/markets" % GAMMA_API_URL
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": 500, "order": "volume", "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        markets = data["data"] if isinstance(data, dict) and "data" in data else data
        if not isinstance(markets, list):
            return []
    except requests.RequestException as exc:
        logger.error("stat_arb gamma fetch fallo: %s", exc)
        return []

    filtered = []
    for m in markets:
        vol = _safe_float(m.get("volume") or m.get("volumeNum"))
        liq = _safe_float(m.get("liquidity") or m.get("liquidityNum"))
        if vol < MIN_VOLUME_USDC or liq < MIN_LIQUIDITY_USDC:
            continue
        price = _extract_yes_price(m)
        if price is None or not (0.02 < price < 0.98):
            continue
        tokens = _extract_token_ids(m)
        if len(tokens) < 2:
            continue
        filtered.append({
            "id": m.get("id") or m.get("conditionId"),
            "question": m.get("question") or m.get("slug"),
            "yes_price": price,
            "yes_token": tokens[0],
            "volume": vol,
        })
        if len(filtered) >= UNIVERSE_SIZE:
            break
    return filtered


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _load_history() -> Dict[str, List[float]]:
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, "r") as f:
            data = json.load(f)
        return {k: list(v)[-MAX_HISTORY_POINTS:] for k, v in data.items()}
    except Exception:
        return {}


def _save_history(history: Dict[str, List[float]]) -> None:
    try:
        with open(HISTORY_PATH, "w") as f:
            json.dump({k: list(v) for k, v in history.items()}, f)
    except Exception as exc:
        logger.warning("stat_arb save_history fallo: %s", exc)


# ---------------------------------------------------------------------------
# StatArb core
# ---------------------------------------------------------------------------
class StatArb:
    def __init__(self, order_manager, allocator: Optional[CapitalAllocator] = None):
        self.om = order_manager
        self.allocator = allocator or CapitalAllocator()
        self.history: Dict[str, List[float]] = defaultdict(
            lambda: deque(maxlen=MAX_HISTORY_POINTS)
        )
        # Reload desde disco
        for k, v in _load_history().items():
            self.history[k] = deque(v, maxlen=MAX_HISTORY_POINTS)
        self.pairs: List[Tuple[str, str, float]] = []  # (id_a, id_b, beta)
        self.positions: Dict[str, Dict[str, Any]] = {}  # market_id -> info
        self.cycle_count = 0
        self.market_meta: Dict[str, Dict[str, Any]] = {}  # id -> last market obj

    # ------------------------------------------------------------------
    def _update_history(self, universe):
        for m in universe:
            mid = m["id"]
            if not mid:
                continue
            self.history[mid].append(m["yes_price"])
            self.market_meta[mid] = m
        _save_history({k: list(v) for k, v in self.history.items()})

    def _recalc_pairs(self):
        ids = [i for i, h in self.history.items() if len(h) >= MIN_SAMPLES]
        found = []
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                xs = list(self.history[a])
                ys = list(self.history[b])
                n = min(len(xs), len(ys))
                xs, ys = xs[-n:], ys[-n:]
                c = _corr(xs, ys)
                if abs(c) >= CORR_THRESHOLD:
                    b_coef = _beta(xs, ys)
                    found.append((a, b, b_coef, c))
        found.sort(key=lambda t: abs(t[3]), reverse=True)
        self.pairs = [(a, b, beta) for a, b, beta, _ in found[:20]]
        logger.info("stat_arb: %d pares correlacionados encontrados", len(self.pairs))

    def _current_zscore(self, id_a, id_b, beta) -> Optional[float]:
        xs = list(self.history[id_a])
        ys = list(self.history[id_b])
        n = min(len(xs), len(ys))
        if n < MIN_SAMPLES:
            return None
        xs, ys = xs[-n:], ys[-n:]
        spreads = [x - beta * y for x, y in zip(xs, ys)]
        mu = _mean(spreads)
        sd = _std(spreads)
        if sd == 0:
            return None
        return (spreads[-1] - mu) / sd

    # ------------------------------------------------------------------
    def _open_position(self, market_id: str, side_market: Dict[str, Any],
                       zscore: float, budget: float):
        if market_id in self.positions:
            return
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            return
        if budget < POSITION_SIZE_USDC:
            log_warning("stat_arb_budget_bajo", module="stat_arb",
                        extra={"budget": budget})
            return
        price = side_market["yes_price"]
        size_tokens = round(POSITION_SIZE_USDC / price, 2)
        if size_tokens < 5:
            return
        try:
            order_ids = self.om.place_limit_buy(
                token_id=side_market["yes_token"],
                price=round(price * 1.005, 2),  # slippage 0.5%
                size=size_tokens,
                market_id=market_id,
                strategy=STRATEGY,
            )
        except Exception as exc:
            logger.error("stat_arb place_limit_buy fallo: %s", exc)
            return
        if not order_ids:
            return
        self.positions[market_id] = {
            "token_id": side_market["yes_token"],
            "entry_price": price,
            "size_tokens": size_tokens,
            "size_usdc": POSITION_SIZE_USDC,
            "z_entry": zscore,
            "opened_at": time.time(),
            "question": side_market.get("question"),
        }
        log_decision(reason="stat_arb_entry", market=side_market.get("question"),
                     strategy=STRATEGY, edge=abs(zscore),
                     size=POSITION_SIZE_USDC,
                     extra={"zscore": zscore, "price": price})

    def _check_exits(self):
        now = time.time()
        for market_id, pos in list(self.positions.items()):
            age_h = (now - pos["opened_at"]) / 3600.0
            # Buscamos la paridad de este market_id para obtener z actual
            z_now = None
            for a, b, beta in self.pairs:
                if market_id in (a, b):
                    z_now = self._current_zscore(a, b, beta)
                    break
            exit_reason = None
            if age_h >= MAX_HOLD_HOURS:
                exit_reason = "max_hold"
            elif z_now is not None and abs(z_now) <= Z_EXIT:
                exit_reason = "z_converged"
            if not exit_reason:
                continue
            try:
                self.om.close_position_market(
                    token_id=pos["token_id"],
                    size=pos["size_tokens"],
                    market_id=market_id,
                    strategy=STRATEGY,
                )
            except Exception as exc:
                logger.error("stat_arb close_position fallo: %s", exc)
                continue
            log_decision(reason="stat_arb_exit_%s" % exit_reason,
                         market=pos.get("question"), strategy=STRATEGY,
                         extra={"age_h": round(age_h, 2), "z_now": z_now})
            self.positions.pop(market_id, None)

    # ------------------------------------------------------------------ main
    def run_cycle(self):
        if not self.allocator.is_enabled(STRATEGY):
            return
        self.cycle_count += 1
        universe = _fetch_universe()
        if not universe:
            return
        self._update_history(universe)

        if self.cycle_count % RECALC_PAIRS_EVERY == 1:
            self._recalc_pairs()

        self._check_exits()

        if not self.pairs:
            return
        budget = self.allocator.get_available(STRATEGY)
        if budget < POSITION_SIZE_USDC:
            return

        # Evalua cada par; compra el lado barato si |z|>Z_ENTRY
        for id_a, id_b, beta in self.pairs:
            if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
                break
            if id_a in self.positions or id_b in self.positions:
                continue
            z = self._current_zscore(id_a, id_b, beta)
            if z is None or abs(z) < Z_ENTRY:
                continue
            # z>0 => A caro vs B => compramos B (barato). z<0 => A barato.
            target_id = id_b if z > 0 else id_a
            market = self.market_meta.get(target_id)
            if not market:
                continue
            self._open_position(target_id, market, z, budget)
            budget -= POSITION_SIZE_USDC

        # Reporta capital desplegado
        deployed = sum(p["size_usdc"] for p in self.positions.values())
        self.allocator.report_deployed(STRATEGY, deployed)
