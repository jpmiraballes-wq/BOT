"""apify_twitter_loop.py — Polling rápido del Apify Twitter scraper desde la Mac.

Bolt+Opus 2026-04-27 noche. Plan "Overtake Swisstony" MOV 3.

Cada 60s (default), invoca el endpoint Base44 `apifyTwitterInjuryWatch` que
ya existe. Ese endpoint maneja toda la lógica: scrape Apify, dedup, severity
classification, alertas Telegram, bulk insert a TwitterInjurySignal.

Solo cambiamos la frecuencia: de cron Base44 cada 5min → loop Mac cada 60s.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests

from config import BASE44_API_KEY, BASE44_APP_ID, BASE44_BASE_URL

logger = logging.getLogger("apify_twitter_loop")

TWITTER_INTERVAL_SECONDS = int(os.environ.get("TWITTER_INTERVAL_SECONDS", "60"))
TWITTER_ENDPOINT = f"{BASE44_BASE_URL}/api/apps/{BASE44_APP_ID}/functions/apifyTwitterInjuryWatch"

_last_run_at: float = 0.0


def run_twitter_loop_once() -> Dict[str, Any]:
    started = time.time()
    try:
        r = requests.post(
            TWITTER_ENDPOINT,
            json={},
            headers={
                "api_key": BASE44_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=120,  # Apify puede tardar
        )
        duration = time.time() - started
        if r.status_code >= 400:
            logger.warning("apify_twitter_loop %d: %s", r.status_code, r.text[:200])
            return {"ok": False, "status": r.status_code, "duration_s": round(duration, 2)}
        result = r.json() if r.text else {}
        if isinstance(result, dict) and result.get("created", 0) > 0:
            logger.info(
                "apify_twitter_loop: %d nuevos tweets (%.1fs)",
                result.get("created", 0), duration,
            )
        return {**(result if isinstance(result, dict) else {}),
                "duration_s": round(duration, 2)}
    except Exception as exc:
        logger.error("apify_twitter_loop fallo: %s", exc)
        return {"ok": False, "error": str(exc)}


def maybe_run_twitter_loop() -> Optional[Dict[str, Any]]:
    """Llamado en cada loop. Solo ejecuta si pasaron TWITTER_INTERVAL_SECONDS."""
    global _last_run_at
    now = time.time()
    if now - _last_run_at < TWITTER_INTERVAL_SECONDS:
        return None
    _last_run_at = now

    try:
        return run_twitter_loop_once()
    except Exception as exc:
        logger.error("apify_twitter_loop wrapper: %s", exc, exc_info=True)
        return None
