"""auto_updater.py — Git auto-pull self-update (AUTO_UPDATE_V1).

Llamado cada ~15min desde main loop. Si hay commits nuevos en origin/main
vs HEAD local, hace 'git pull --ff-only' y devuelve True. El loop principal
al ver True hace sys.exit(0) y el wrapper (run_bot.sh) relanza con código
fresco.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("auto_updater")

# El repo está un nivel arriba de bot-v2/
REPO_DIR = Path(__file__).resolve().parent.parent
CHECK_INTERVAL_SEC = 15 * 60  # 15 minutos

_last_check_at: float = 0.0


def _run_git(args: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return 1, f"exc: {exc}"


def check_and_update() -> bool:
    """Returns True si hubo update exitoso y el bot debe reiniciarse."""
    global _last_check_at
    now = time.time()
    if now - _last_check_at < CHECK_INTERVAL_SEC:
        return False
    _last_check_at = now

    # 1) git fetch (si falla red, ignoramos y seguimos)
    rc, out = _run_git(["fetch", "origin", BRANCH_NAME])
    if rc != 0:
        logger.warning("auto_updater: git fetch fallo (%s). Seguimos sin update.", out[:200])
        return False

    # 2) Comparar HEAD local vs origin/main
    rc_local, local_sha = _run_git(["rev-parse", "HEAD"])
    rc_remote, remote_sha = _run_git(["rev-parse", f"origin/{BRANCH_NAME}"])
    if rc_local != 0 or rc_remote != 0:
        logger.warning("auto_updater: rev-parse fallo. local=%s remote=%s", local_sha, remote_sha)
        return False
    if local_sha.strip() == remote_sha.strip():
        return False  # al día

    logger.info(
        "auto_updater: hay update disponible. local=%s remote=%s",
        local_sha[:8], remote_sha[:8],
    )

    # 3) git pull --ff-only (no mergeamos a ciegas)
    rc_pull, out_pull = _run_git(["pull", "--ff-only", "origin", BRANCH_NAME], timeout=40)
    if rc_pull != 0:
        logger.error("auto_updater: git pull fallo: %s", out_pull[:300])
        return False

    logger.info("auto_updater: pull OK. Señalando reinicio.")
    return True


# Branch por env var para poder probar en otra branch si hace falta.
BRANCH_NAME = os.environ.get("BOT_BRANCH", "main")
