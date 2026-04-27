"""reporter.py - Envia heartbeats de SystemState a Base44.

Crea un nuevo registro en cada heartbeat (POST). El dashboard lee el mas
reciente con sort=-created_date, limit=1.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

try:
    from config import BOT_VERSION
except ImportError:
    BOT_VERSION = "v2"

try:
    from config import REPORT_INTERVAL_SECONDS
except ImportError:
    REPORT_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 15


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class Reporter:
    def __init__(self):
        self._last_report_ts = 0.0
        # REPORTER_UPSERT_V2 — cache del id del singleton propio. None hasta el primer GET/POST.
        self._singleton_id = None
        self._endpoint = "%s/api/apps/%s/entities/SystemState" % (
            BASE44_BASE_URL, BASE44_APP_ID,
        )
        self._headers = {
            "api_key": BASE44_API_KEY or "",
            "Content-Type": "application/json",
        }

    def _due(self):
        return (time.time() - self._last_report_ts) >= REPORT_INTERVAL_SECONDS

    def report(self, snapshot, force=False):
        if not BASE44_API_KEY:
            return
        if not force and not self._due():
            return

        payload = {
            "mode": snapshot.get("mode", "running"),
            "capital_total": float(snapshot.get("capital_total") or 0.0),
            "capital_deployed": float(snapshot.get("capital_deployed") or 0.0),
            "daily_pnl": float(snapshot.get("daily_pnl") or 0.0),
            "total_pnl": float(snapshot.get("total_pnl") or 0.0),
            "drawdown_pct": float(snapshot.get("drawdown_pct") or 0.0),
            "win_rate": float(snapshot.get("win_rate") or 0.0),
            "open_positions": int(snapshot.get("open_positions") or 0),
            "total_trades": int(snapshot.get("total_trades") or 0),
            "uptime_hours": float(snapshot.get("uptime_hours") or 0.0),
            "last_heartbeat": _now_iso(),
            "heartbeat_at": _now_iso(),
            "bot_version": BOT_VERSION,
            "notes": str(snapshot.get("notes") or ""),
        }

        # REPORTER_UPSERT_V2 — cache self._singleton_id en memoria.
        # GET solo la primera vez (o si el PUT da 404 = record borrado).
        # Resuelve race del VPS Shadow cuando el Mac domina el top-20 del sort
        # -created_date y el VPS no se ve a sí mismo en la lista.
        try:
            singleton_id = self._singleton_id
            if singleton_id is None:
                singleton_id = self._find_singleton_id(payload)
                if singleton_id:
                    self._singleton_id = singleton_id
                    logger.info("Reporter: singleton existente cacheado id=%s", singleton_id)
            if singleton_id:
                put_url = "%s/%s" % (self._endpoint, singleton_id)
                resp = requests.put(put_url, json=payload,
                                    headers=self._headers, timeout=REQUEST_TIMEOUT)
                # Si el record fue borrado server-side, invalidar cache y reintentar como POST.
                if resp.status_code == 404:
                    logger.warning("Singleton id=%s 404, invalidando cache y POSTeando nuevo", singleton_id)
                    self._singleton_id = None
                    resp = requests.post(self._endpoint, json=payload,
                                         headers=self._headers, timeout=REQUEST_TIMEOUT)
                    if resp.status_code < 400:
                        try:
                            new_id = (resp.json() or {}).get("id")
                            if new_id:
                                self._singleton_id = new_id
                        except Exception:
                            pass
            else:
                resp = requests.post(self._endpoint, json=payload,
                                     headers=self._headers, timeout=REQUEST_TIMEOUT)
                if resp.status_code < 400:
                    try:
                        new_id = (resp.json() or {}).get("id")
                        if new_id:
                            self._singleton_id = new_id
                            logger.info("Reporter: singleton nuevo creado id=%s", new_id)
                    except Exception:
                        pass
            if resp.status_code >= 400:
                logger.error("Error reportando a Base44: %d %s",
                             resp.status_code, resp.text[:200])
                return
            self._last_report_ts = time.time()
            logger.info("Heartbeat OK - capital: %s (singleton=%s)",
                        payload["capital_total"], self._singleton_id or "new")
        except requests.RequestException as exc:
            logger.error("Error reportando a Base44: %s", exc)

    def _find_singleton_id(self, payload):
        """REPORTER_UPSERT_V2 — busca el record propio (mismo bot_id) más reciente.

        Solo se llama 1 vez al arrancar el bot (y de nuevo si el cache se
        invalida por 404). El bot_id se infiere del campo notes:
          - notes startswith 'shadow:' → VPS Shadow
          - cualquier otro caso → Mac (main)
        Devuelve el id o None si todavía no existe (primer heartbeat).
        """
        try:
            is_shadow = str(payload.get("notes") or "").startswith("shadow:")
            # Limit alto para que el VPS encuentre su record aunque el Mac
            # haya creado decenas de records nuevos arriba en el sort.
            list_url = self._endpoint + "?sort=-created_date&limit=200"
            resp = requests.get(list_url, headers=self._headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                return None
            records = resp.json() or []
            if isinstance(records, dict):
                records = records.get("data") or records.get("records") or []
            for rec in records:
                rec_notes = str(rec.get("notes") or "")
                rec_is_shadow = rec_notes.startswith("shadow:")
                if rec_is_shadow == is_shadow:
                    return rec.get("id")
            return None
        except Exception as exc:
            logger.warning("_find_singleton_id failed: %s (will fallback to POST)", exc)
            return None

    def send_minimal_heartbeat(self, mode="running", notes=""):
        """REPORTER_ZOMBIE_GUARD_V1
        Heartbeat de fallback: ANTES posteaba SystemState con todos los campos
        en 0 cuando build_snapshot() fallaba. Eso causaba el "reporter zombie":
        cada ~5min el dashboard veia datos vacios que pisaban el snapshot bueno.

        FIX 2026-04-27 JP+Opus: este metodo ya NO escribe ceros. Solo loggea
        que el snapshot fallo. El dashboard mantiene el ultimo registro bueno
        (con last_heartbeat un poco viejo) hasta que el proximo build_snapshot
        funcione y mande datos reales.

        Si necesitas avisar al dashboard que el bot esta vivo pero sin datos,
        agregar un campo SystemState.is_partial=True y hacer PATCH al ultimo
        registro en vez de POST nuevo. Por ahora, lo mas seguro es no hacer nada.
        """
        if not BASE44_API_KEY:
            return
        logger.warning(
            "REPORTER_ZOMBIE_GUARD: build_snapshot fallo, NO escribo ceros. notes=%s",
            str(notes)[:100],
        )
        return  # No mas zombies pisando el dashboard.

    def _OLD_send_minimal_heartbeat_DEPRECATED(self, mode="running", notes=""):
        """Codigo viejo preservado por si JP quiere revertir. NO se usa."""
        if not BASE44_API_KEY:
            return
        payload = {
            "mode": mode,
            "capital_total": 0.0,
            "capital_deployed": 0.0,
            "daily_pnl": 0.0,
            "total_pnl": 0.0,
            "drawdown_pct": 0.0,
            "win_rate": 0.0,
            "open_positions": 0,
            "total_trades": 0,
            "last_heartbeat": _now_iso(),
            "heartbeat_at": _now_iso(),
            "bot_version": BOT_VERSION,
            "notes": "minimal_hb:" + str(notes)[:100],
        }
        try:
            resp = requests.post(self._endpoint, json=payload,
                                 headers=self._headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                logger.error("minimal_hb HTTP %d: %s",
                             resp.status_code, resp.text[:200])
                return
            self._last_report_ts = time.time()
            logger.warning("Heartbeat MINIMO enviado (build_snapshot fallo)")
        except requests.RequestException as exc:
            logger.error("minimal_hb request fallo: %s", exc)
