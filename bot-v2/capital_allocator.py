"""capital_allocator.py - Gestor de capital por estrategia.

Lee y actualiza la entity StrategyCapital de Base44. Garantiza aislamiento
total de capital entre estrategias: ninguna puede desplegar mas de su
'allocated' configurado en el dashboard.

Uso tipico desde una estrategia:

    from capital_allocator import CapitalAllocator
    ca = CapitalAllocator()
    budget = ca.get_available("market_making")   # allocated - deployed
    if budget <= 0:
        return  # no hay mas capital para esta estrategia
    ...
    ca.report_deployed("market_making", deployed_usdc)
    ca.record_trade("market_making", pnl=1.23)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15
CACHE_TTL_SECONDS = 30  # evita bombardear la API: leemos como mucho cada 30s


class CapitalAllocator:
    """Fachada sobre la entity StrategyCapital.

    Guarda un cache en memoria de CACHE_TTL_SECONDS para evitar hits en
    cada ciclo. Las escrituras invalidan el cache inmediatamente.
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ts: float = 0.0

    # ---------------------------------------------------------------- http
    @staticmethod
    def _headers():
        return {
            "api_key": BASE44_API_KEY or "",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _endpoint(record_id: Optional[str] = None) -> str:
        base = "%s/api/apps/%s/entities/StrategyCapital" % (
            BASE44_BASE_URL, BASE44_APP_ID,
        )
        return "%s/%s" % (base, record_id) if record_id else base

    # ---------------------------------------------------------------- read
    def _refresh(self, force: bool = False) -> None:
        if not force and (time.time() - self._cache_ts) < CACHE_TTL_SECONDS:
            return
        if not BASE44_API_KEY:
            logger.warning("BASE44_API_KEY ausente; CapitalAllocator sin datos.")
            return
        try:
            resp = requests.get(self._endpoint(), headers=self._headers(),
                                timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("StrategyCapital list %d: %s",
                             resp.status_code, resp.text[:200])
                return  # mantiene cache previo
            data = resp.json()
            records = data["data"] if isinstance(data, dict) and "data" in data else data
            if not isinstance(records, list):
                logger.warning("StrategyCapital response inesperada: %s", type(records).__name__)
                return  # mantiene cache previo
            new_cache = {r["name"]: r for r in records if r.get("name")}
            if not new_cache:
                logger.warning("StrategyCapital devolvio 0 registros; conservando cache previo (%d).",
                               len(self._cache))
                return  # evita quedar con cache vacio
            self._cache = new_cache
            self._cache_ts = time.time()
        except requests.RequestException as exc:
            logger.error("CapitalAllocator refresh fallo: %s", exc)
            # mantiene cache previo

    def get(self, strategy: str) -> Optional[Dict[str, Any]]:
        self._refresh()
        # Si falto la estrategia en cache, forzamos un refresh para no devolver None
        # por un cache vacio (p.ej. tras un error transiente del API).
        if strategy not in self._cache:
            self._refresh(force=True)
        return self._cache.get(strategy)

    def is_enabled(self, strategy: str) -> bool:
        rec = self.get(strategy)
        return bool(rec and rec.get("enabled"))

    def get_allocated(self, strategy: str) -> float:
        rec = self.get(strategy)
        # La entity externa usa 'allocated_usdc'. Fallback a 'allocated' por compat.
        if not rec:
            return 0.0
        return float(rec.get("allocated_usdc") or rec.get("allocated") or 0.0)

    def get_deployed(self, strategy: str) -> float:
        rec = self.get(strategy)
        if not rec:
            return 0.0
        return float(rec.get("deployed_usdc") or rec.get("deployed") or 0.0)

    def get_available(self, strategy: str) -> float:
        """Capital disponible = allocated - deployed. Nunca negativo."""
        rec = self.get(strategy)
        if not rec or not rec.get("enabled"):
            return 0.0
        allocated = float(rec.get("allocated_usdc") or rec.get("allocated") or 0.0)
        deployed = float(rec.get("deployed_usdc") or rec.get("deployed") or 0.0)
        return max(0.0, allocated - deployed)

    def list_enabled(self) -> List[Dict[str, Any]]:
        self._refresh()
        return [r for r in self._cache.values() if r.get("enabled")]

    # --------------------------------------------------------------- write
    def _put(self, record_id: str, payload: Dict[str, Any]) -> bool:
        """Base44 API no soporta PATCH; hay que hacer PUT con el doc completo.

        IMPORTANTE: la Base44 external API devuelve {} en GET por id
        individual, asi que no podemos releer el record antes del PUT
        (eso borraba allocated_usdc). Mergeamos sobre self._cache que
        ya tiene el record completo tras el _refresh inicial.
        """
        try:
            # Asegura cache fresco y localiza el record por id.
            self._refresh()
            current = None
            for rec in self._cache.values():
                if rec.get("id") == record_id:
                    current = dict(rec)
                    break
            if current is None:
                logger.error("StrategyCapital _put: record %s no esta en cache", record_id)
                return False
            # Quitar campos de solo-lectura que rechaza el PUT.
            for k in ("id", "created_date", "updated_date", "created_by",
                      "created_by_id", "is_sample"):
                current.pop(k, None)
            current.update(payload)
            resp = requests.put(
                self._endpoint(record_id), json=current,
                headers=self._headers(), timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code >= 400:
                logger.error("StrategyCapital put %d: %s",
                             resp.status_code, resp.text[:200])
                return False
            self._cache_ts = 0.0
            return True
        except requests.RequestException as exc:
            logger.error("StrategyCapital put fallo: %s", exc)
            return False

    def report_deployed(self, strategy: str, deployed_usdc: float,
                        notes: Optional[str] = None) -> bool:
        """Actualiza el capital desplegado actual de una estrategia."""
        rec = self.get(strategy)
        if not rec:
            logger.warning("StrategyCapital sin registro para %s", strategy)
            return False
        payload: Dict[str, Any] = {
            "deployed_usdc": float(deployed_usdc),
            "last_run_at": datetime.now(timezone.utc).isoformat(),
        }
        if notes is not None:
            payload["notes"] = notes
        return self._put(rec["id"], payload)

    def record_trade(self, strategy: str, pnl: float) -> bool:
        """Suma pnl al pnl_today/total e incrementa contadores."""
        rec = self.get(strategy)
        if not rec:
            logger.warning("StrategyCapital sin registro para %s", strategy)
            return False
        payload = {
            "pnl_today": float(rec.get("pnl_today") or 0.0) + float(pnl),
            "pnl_total": float(rec.get("pnl_total") or 0.0) + float(pnl),
            "trades_today": int(rec.get("trades_today") or 0) + 1,
            "trades_total": int(rec.get("trades_total") or 0) + 1,
        }
        return self._put(rec["id"], payload)

    def reset_daily(self) -> int:
        """Resetea pnl_today y trades_today (llamar a las 00:00 UTC)."""
        self._refresh(force=True)
        count = 0
        for rec in list(self._cache.values()):
            if self._put(rec["id"], {"pnl_today": 0.0, "trades_today": 0}):
                count += 1
        return count
