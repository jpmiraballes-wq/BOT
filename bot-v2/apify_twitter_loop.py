"""apify_twitter_loop.py — DISABLED 2026-04-27.

El loop local cada 60s era redundante: el cron cloud apifyTwitterInjuryWatch
en Base44 ya corre cada 5min y persiste TwitterInjurySignal correctamente.

El loop local tiraba 403 por config de URL mal cargada. En vez de perseguir
el bug, eliminamos la duplicación. El cron cloud sigue intacto.

Si en el futuro queremos polling más rápido que 5min, restauramos este archivo.
"""
# NO exportamos maybe_run_twitter_loop a propósito.
# main.py tiene try/except ImportError envolviendo el import.
