"""news_trading.py - AI News Trading (Fase 2 dia 1).

Flujo por ciclo (cada 60s desde main.py):
  1) fetch RSS de Reuters, AP, BBC (todas gratis)
  2) para cada titulo nuevo (no visto antes): hash y pasa a buffer
  3) cada hash guarda {sources, first_seen_ts, classifications}
  4) cuando un titulo (o uno muy similar) aparece en >=2 fuentes dentro
     de una ventana de 5 min, lo marcamos como 'confirmed'
  5) si confidence_promedio >= 0.70 y news_trading tiene capital,
     ejecutamos BUY en CLOB al mejor ask
  6) registramos la posicion con expiry = now + decay_min OR 8h (lo menor)
  7) en cada ciclo revisamos posiciones abiertas: vendemos si expired
     o si el precio ya toco el take-profit calculado

Persistencia: usamos entities en Base44 para rastrear:
  - NewsSignal: cada noticia clasificada (diagnostico/auditoria)
  - NewsPosition: posiciones abiertas por la estrategia (se cierran en 8h max)
"""

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

from capital_allocator import CapitalAllocator
from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL, GAMMA_API_URL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STRATEGY_NAME = "news_trading"
REQUEST_TIMEOUT = 15
CLOB_BASE_URL = "https://clob.polymarket.com"

# RSS sources - todas gratis sin API key
RSS_SOURCES = [
    ("reuters_world", "https://feeds.reuters.com/reuters/worldNews"),
    ("reuters_business", "https://feeds.reuters.com/reuters/businessNews"),
    ("reuters_politics", "https://feeds.reuters.com/Reuters/PoliticsNews"),
    ("ap_topnews", "https://rsshub.app/apnews/topics/apf-topnews"),
    ("ap_politics", "https://rsshub.app/apnews/topics/apf-politics"),
    ("bbc_world", "http://feeds.bbci.co.uk/news/world/rss.xml"),
    ("bbc_business", "http://feeds.bbci.co.uk/news/business/rss.xml"),
]

# Umbrales de decision
MIN_CONFIDENCE = 0.70
MIN_SOURCES_CONFIRM = 2
CONFIRMATION_WINDOW_SEC = 5 * 60        # 5 min
MAX_HOLD_SECONDS = 8 * 60 * 60          # 8h hard-close
NEWS_POLL_INTERVAL_SEC = 60
DEDUPE_TTL_SEC = 6 * 60 * 60            # olvida hashes viejos a las 6h
MAX_POSITIONS_CONCURRENT = 4             # safety: no mas de 4 posiciones news a la vez
MIN_SHARE_SIZE_USDC = 5.0                # no entrar con menos de $5
KELLY_BASE_FRACTION = 0.25               # base kelly; se multiplica por conf^2
SIMILARITY_TOKEN_OVERLAP = 0.6           # 60% palabras comunes = misma noticia


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _title_hash(title: str) -> str:
    norm = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _title_tokens(title: str) -> set:
    norm = re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower())
    return {t for t in norm.split() if len(t) > 3}


def _similar(a: str, b: str) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    smaller = min(len(ta), len(tb))
    return (inter / smaller) >= SIMILARITY_TOKEN_OVERLAP


# ---------------------------------------------------------------------------
# RSS fetch
# ---------------------------------------------------------------------------
def _fetch_rss(source_name: str, url: str) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0 NewsTradingBot/1.0"})
        if resp.status_code != 200:
            logger.debug("RSS %s HTTP %d", source_name, resp.status_code)
            return []
        root = ET.fromstring(resp.content)
        items = []
        for item in root.iter("item"):
            title_el = item.find("title")
            desc_el = item.find("description")
            pub_el = item.find("pubDate")
            if title_el is None or not (title_el.text or "").strip():
                continue
            items.append({
                "source": source_name,
                "title": title_el.text.strip(),
                "description": (desc_el.text or "").strip() if desc_el is not None else "",
                "pub_date": (pub_el.text or "").strip() if pub_el is not None else "",
            })
        return items
    except (requests.RequestException, ET.ParseError) as exc:
        logger.debug("RSS %s fallo: %s", source_name, exc)
        return []


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------
def _fetch_top_markets(limit: int = 200) -> List[Dict[str, Any]]:
    url = "%s/markets" % GAMMA_API_URL
    params = {
        "active": "true", "closed": "false", "archived": "false",
        "limit": limit, "order": "volume", "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data if isinstance(data, list) else []
    except requests.RequestException:
        return []


def _summary_market_list(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Devuelve una lista compacta para dar al LLM sin saturar contexto."""
    out = []
    for m in markets:
        q = m.get("question") or m.get("slug")
        mid = m.get("id") or m.get("conditionId")
        if not q or not mid:
            continue
        out.append({"id": str(mid), "q": q[:160]})
    return out[:80]  # 80 mercados maximo al prompt


def _classify_with_llm(news_title: str, news_desc: str,
                      market_snapshot: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Usa el InvokeLLM de Base44 (endpoint integracion Core).

    Devuelve None si no clasifica. Si clasifica:
      {market_id, direction ('yes'/'no'), confidence 0-1, decay_minutes, rationale}
    """
    if not BASE44_API_KEY:
        return None
    url = "%s/api/apps/%s/integrations/Core/InvokeLLM" % (BASE44_BASE_URL, BASE44_APP_ID)

    prompt = (
        "You are a prediction-market news analyst. Given a breaking-news headline "
        "and a list of live Polymarket markets, decide if this news should move any "
        "market's YES probability materially in the next minutes/hours.\n\n"
        "NEWS HEADLINE: " + news_title + "\n"
        "NEWS DESCRIPTION: " + (news_desc or "") + "\n\n"
        "ACTIVE MARKETS (id + question):\n" +
        json.dumps(market_snapshot, ensure_ascii=False) +
        "\n\nReturn JSON. If no market is materially affected, return "
        "{\"relevant\": false}. Otherwise return:\n"
        "{\"relevant\": true, \"market_id\": \"<id>\", "
        "\"direction\": \"yes\"|\"no\", "
        "\"confidence\": 0.0-1.0, \"decay_minutes\": int, "
        "\"rationale\": \"<short>\"}\n"
        "Rules: confidence >= 0.7 only when the news DIRECTLY confirms/denies "
        "the market's resolution criterion. decay_minutes is how long the edge "
        "is likely to last before the market re-prices (30-480)."
    )

    schema = {
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "market_id": {"type": "string"},
            "direction": {"type": "string", "enum": ["yes", "no"]},
            "confidence": {"type": "number"},
            "decay_minutes": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["relevant"],
    }
    try:
        resp = requests.post(
            url,
            json={"prompt": prompt, "response_json_schema": schema},
            headers={"api_key": BASE44_API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            logger.warning("InvokeLLM %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        # Base44 puede devolver el contenido en data['output'] o raiz
        result = data.get("output") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            result = data if isinstance(data, dict) else None
        if not result or not result.get("relevant"):
            return None
        return {
            "market_id": str(result.get("market_id") or "").strip(),
            "direction": (result.get("direction") or "yes").lower(),
            "confidence": _safe_float(result.get("confidence"), 0.0),
            "decay_minutes": int(_safe_float(result.get("decay_minutes"), 60)),
            "rationale": result.get("rationale") or "",
        }
    except requests.RequestException as exc:
        logger.warning("InvokeLLM fallo: %s", exc)
        return None


# ---------------------------------------------------------------------------
# CLOB helpers (ejecucion)
# ---------------------------------------------------------------------------
def _fetch_market_full(market_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get("%s/markets/%s" % (GAMMA_API_URL, market_id),
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data
        return None
    except requests.RequestException:
        return None


def _extract_token_ids(market: Dict[str, Any]) -> List[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    return []


def _fetch_best_ask(token_id: str) -> Tuple[Optional[float], float]:
    try:
        resp = requests.get("%s/book" % CLOB_BASE_URL,
                            params={"token_id": token_id},
                            timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None, 0.0
        data = resp.json()
        asks = data.get("asks") or []
        parsed = [(float(a.get("price", 0)), float(a.get("size", 0))) for a in asks]
        parsed = [p for p in parsed if p[0] > 0 and p[1] > 0]
        if not parsed:
            return None, 0.0
        parsed.sort(key=lambda x: x[0])
        return parsed[0][0], parsed[0][1]
    except (requests.RequestException, ValueError, TypeError):
        return None, 0.0


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
@dataclass
class _HeadlineState:
    title: str
    sources: set = field(default_factory=set)
    first_seen: float = 0.0
    classifications: List[Dict[str, Any]] = field(default_factory=list)
    executed: bool = False


class NewsTrader:
    """Ciclo completo de news trading. Se instancia una vez desde main.py."""

    def __init__(self, order_manager, capital_allocator: CapitalAllocator):
        self.om = order_manager
        self.ca = capital_allocator
        self._headlines: Dict[str, _HeadlineState] = {}
        self._last_poll_ts: float = 0.0
        self._positions: List[Dict[str, Any]] = []  # in-memory; persistidas ademas

    # ------------------------------------------------------------ main loop
    def run_cycle(self) -> None:
        """Invocar desde main.py en cada vuelta. No bloquea si no toca pollear."""
        now = _now()
        # Siempre gestiona salidas primero (max 8h hold)
        self._manage_positions()

        if now - self._last_poll_ts < NEWS_POLL_INTERVAL_SEC:
            return
        self._last_poll_ts = now

        if not self.ca.is_enabled(STRATEGY_NAME):
            return
        if self.ca.get_available(STRATEGY_NAME) < MIN_SHARE_SIZE_USDC:
            return

        self._gc_old_headlines()
        new_items = self._poll_all_feeds()
        if not new_items:
            return

        # Clasifica solo titulares totalmente nuevos (primera vista)
        markets_snapshot = _summary_market_list(_fetch_top_markets())
        for item in new_items:
            self._classify_and_record(item, markets_snapshot)

        # Busca confirmaciones y ejecuta
        self._check_and_execute()

    # --------------------------------------------------------- headlines io
    def _poll_all_feeds(self) -> List[Dict[str, Any]]:
        """Devuelve SOLO items nuevos (no vistos antes en este runtime)."""
        new_items = []
        for name, url in RSS_SOURCES:
            for item in _fetch_rss(name, url):
                h = _title_hash(item["title"])
                existing = self._headlines.get(h)
                if existing is None:
                    # Busca por similitud con hashes recientes para merge
                    merged = False
                    for other_h, other in self._headlines.items():
                        if _similar(item["title"], other.title):
                            if name not in other.sources:
                                other.sources.add(name)
                            merged = True
                            break
                    if merged:
                        continue
                    self._headlines[h] = _HeadlineState(
                        title=item["title"],
                        sources={name},
                        first_seen=_now(),
                    )
                    new_items.append(item)
                else:
                    existing.sources.add(name)
        if new_items:
            logger.info("news_trading: %d titulares nuevos", len(new_items))
        return new_items

    def _gc_old_headlines(self) -> None:
        cutoff = _now() - DEDUPE_TTL_SEC
        stale = [h for h, st in self._headlines.items() if st.first_seen < cutoff]
        for h in stale:
            del self._headlines[h]

    # ---------------------------------------------------------- classifier
    def _classify_and_record(self, item: Dict[str, Any],
                             markets_snapshot: List[Dict[str, Any]]) -> None:
        h = _title_hash(item["title"])
        state = self._headlines.get(h)
        if not state:
            return

        cls = _classify_with_llm(item["title"], item.get("description", ""),
                                 markets_snapshot)
        if not cls or not cls.get("market_id"):
            return
        if cls["confidence"] < 0.5:
            # descartamos ruidosas desde el arranque
            return
        state.classifications.append({
            **cls,
            "source": item["source"],
            "ts": _now(),
        })
        self._persist_signal(item, cls)

    def _persist_signal(self, item: Dict[str, Any], cls: Dict[str, Any]) -> None:
        if not BASE44_API_KEY:
            return
        url = "%s/api/apps/%s/entities/Signal" % (BASE44_BASE_URL, BASE44_APP_ID)
        try:
            requests.post(url, json={
                "market": cls.get("market_id") or "unknown",
                "strategy": STRATEGY_NAME,
                "spread_pct": 0.0,
                "confidence": float(cls.get("confidence") or 0.0),
                "status": "pending",
            }, headers={"api_key": BASE44_API_KEY,
                        "Content-Type": "application/json"},
               timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            pass

    # ---------------------------------------------------------- execution
    def _check_and_execute(self) -> None:
        now = _now()
        for h, state in list(self._headlines.items()):
            if state.executed:
                continue
            if len(state.sources) < MIN_SOURCES_CONFIRM:
                continue
            # Ventana de confirmacion
            if now - state.first_seen > CONFIRMATION_WINDOW_SEC:
                state.executed = True  # expira la ventana
                continue
            if not state.classifications:
                continue

            # Agrupar por market_id y promediar confidence
            by_market: Dict[str, List[Dict[str, Any]]] = {}
            for c in state.classifications:
                mid = c.get("market_id")
                if mid:
                    by_market.setdefault(mid, []).append(c)
            if not by_market:
                continue

            # Escoge el mercado con mayor confidence promedio
            market_id, cls_list = max(
                by_market.items(),
                key=lambda kv: sum(c["confidence"] for c in kv[1]) / len(kv[1])
            )
            avg_conf = sum(c["confidence"] for c in cls_list) / len(cls_list)
            if avg_conf < MIN_CONFIDENCE:
                continue

            # Direccion mayoritaria
            yes_votes = sum(1 for c in cls_list if c["direction"] == "yes")
            no_votes = len(cls_list) - yes_votes
            direction = "yes" if yes_votes >= no_votes else "no"
            decay_min = int(sum(c["decay_minutes"] for c in cls_list) / len(cls_list))

            ok = self._execute_entry(state, market_id, direction, avg_conf, decay_min)
            state.executed = True
            if ok:
                logger.info("news_trading EXECUTED %s %s conf=%.2f decay=%dm",
                            market_id, direction, avg_conf, decay_min)

    def _execute_entry(self, state: _HeadlineState, market_id: str,
                       direction: str, confidence: float, decay_min: int) -> bool:
        if len(self._positions) >= MAX_POSITIONS_CONCURRENT:
            return False
        budget = self.ca.get_available(STRATEGY_NAME)
        if budget < MIN_SHARE_SIZE_USDC:
            return False

        market = _fetch_market_full(market_id)
        if not market:
            return False
        token_ids = _extract_token_ids(market)
        if len(token_ids) < 2:
            return False
        # token 0 = YES, token 1 = NO por convencion Polymarket
        token_id = token_ids[0] if direction == "yes" else token_ids[1]

        ask_price, ask_size = _fetch_best_ask(token_id)
        if ask_price is None or ask_price >= 0.95:
            return False  # no vale la pena por upside restante

        # Kelly base * confidence^2, limitado por capital disponible y por book
        kelly_size_usdc = budget * KELLY_BASE_FRACTION * (confidence ** 2)
        book_cap_usdc = ask_size * ask_price * 0.5  # no arrasar book (50% del tope)
        size_usdc = min(kelly_size_usdc, book_cap_usdc, budget)
        if size_usdc < MIN_SHARE_SIZE_USDC:
            return False

        # Ejecuta BUY limite al mejor ask via OrderManager
        shares = size_usdc / ask_price
        order_id = self.om.place_limit_buy(token_id, ask_price, shares,
                                          strategy=STRATEGY_NAME)
        if not order_id:
            return False

        expiry_sec = min(decay_min * 60, MAX_HOLD_SECONDS)
        position = {
            "market_id": market_id,
            "token_id": token_id,
            "direction": direction,
            "entry_price": ask_price,
            "shares": shares,
            "size_usdc": size_usdc,
            "opened_at": _now(),
            "expires_at": _now() + expiry_sec,
            "confidence": confidence,
            "order_id": order_id,
            "headline": state.title[:200],
        }
        self._positions.append(position)
        self.ca.report_deployed(STRATEGY_NAME,
                               sum(p["size_usdc"] for p in self._positions))
        return True

    # ----------------------------------------------------------- exits
    def _manage_positions(self) -> None:
        if not self._positions:
            return
        now = _now()
        still_open = []
        for p in self._positions:
            # Hard-close por expiracion o >8h
            if now >= p["expires_at"]:
                self._close_position(p, reason="expired")
                continue
            # TP dinamico: si el precio subio >= +15% sobre entry, realiza
            current_ask, _ = _fetch_best_ask(p["token_id"])
            if current_ask is not None and current_ask >= p["entry_price"] * 1.15:
                self._close_position(p, reason="take_profit", exit_price=current_ask)
                continue
            still_open.append(p)
        self._positions = still_open
        self.ca.report_deployed(STRATEGY_NAME,
                               sum(p["size_usdc"] for p in self._positions))

    def _close_position(self, p: Dict[str, Any], reason: str,
                       exit_price: Optional[float] = None) -> None:
        # Sell market (best bid) via OrderManager; si falla, log y sigue.
        result = self.om.close_position_market(p["token_id"], p["shares"],
                                              strategy=STRATEGY_NAME)
        if not result:
            logger.warning("news_trading close fallo token=%s", p["token_id"])
            return
        fill_price = exit_price if exit_price is not None else result.get("avg_price")
        if fill_price is None:
            return
        pnl = (fill_price - p["entry_price"]) * p["shares"]
        self.ca.record_trade(STRATEGY_NAME, pnl=pnl)
        logger.info("news_trading CLOSE %s reason=%s pnl=%.2f entry=%.3f exit=%.3f",
                    p["market_id"], reason, pnl, p["entry_price"], fill_price)
