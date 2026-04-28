"""risk_manager.py - Controles de riesgo del bot.

Responsabilidades:
  - Track del high-watermark y del drawdown global
  - Stop global si drawdown > MAX_DRAWDOWN_PCT
  - Stop por posicion si perdida > MAX_LOSS_PER_POSITION_USDC
  - Cap absoluto de exposicion total
  - Creacion del archivo shutdown.flag para detener el bot
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple

from config import (
    CAPITAL_USDC,
    MAX_DRAWDOWN_PCT,
    MAX_LOSS_PER_POSITION_USDC,
    MAX_TOTAL_EXPOSURE_USDC,
    RESERVE_PCT,
    MAX_POSITION_PCT,
    SHUTDOWN_FLAG_PATH,
    STATE_FILE_PATH,
)

logger = logging.getLogger(__name__)


class RiskManager:
    """Guardia de riesgo. Persistente via state.json.

    RM_NO_FAKE_CAPITAL_V1: __init__ AHORA requiere initial_capital real desde BotConfig.
    Si initial_capital <= 0 levanta ValueError. No mas fallback fantasma a 0.
    """

    def __init__(self, initial_capital: float) -> None:
        try:
            cap = float(initial_capital)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "RiskManager: initial_capital debe ser numerico, recibido %r" % (initial_capital,)
            ) from exc
        if cap <= 0:
            raise ValueError(
                "RiskManager: initial_capital debe ser > 0, recibido %.4f. "
                "Setea BotConfig.capital_usdc en el dashboard." % cap
            )
        self.state_path: Path = STATE_FILE_PATH
        self.high_watermark: float = cap
        self.current_equity: float = cap
        self.daily_pnl: float = 0.0
        self.daily_anchor_equity: float = cap
        self.daily_anchor_ts: float = time.time()
        self.halted: bool = False
        self.halt_reason: str = ""
        self._load()

    def set_dynamic_max_position_pct(self, pct) -> None:
        """DYNAMIC_MAX_POSITION_PCT_V1: permite a main.py setear el cap
        dinamico de position size leido desde BotConfig.max_position_pct.
        Si pct es None o invalido, se usa MAX_POSITION_PCT del config.
        """
        try:
            val = float(pct) if pct is not None else None
            if val is not None and 0 < val <= 0.5:
                self._dynamic_max_position_pct = val
                return
        except (TypeError, ValueError):
            pass
        self._dynamic_max_position_pct = None

    def update_capital(self, live_capital) -> None:
        """Sincroniza current_equity con BotConfig.capital_usdc (override total).

        BotConfig es la fuente de verdad del capital configurado por el
        usuario. Siempre se sobreescribe (sube o baja). El high_watermark
        solo sube para preservar el tope historico.
        """
        if live_capital is None:
            return
        try:
            live = float(live_capital)
        except (TypeError, ValueError):
            return
        if live <= 0:
            return
        changed = False
        if abs(live - self.current_equity) > 0.01:
            self.current_equity = live
            changed = True
        if live > self.high_watermark:
            self.high_watermark = live
            changed = True
        if changed:
            self._save()
            logger.info("RiskManager capital override -> equity=%.2f hwm=%.2f",
                        self.current_equity, self.high_watermark)

    # --------------------------------------------------------- persistencia
    def _load(self) -> None:
        # RM_TRUST_CONSTRUCTOR_V1: si state.json esta cacheado con un capital DESFASADO
        # (>5%) del que vino por el constructor (BotConfig real), lo ignoramos
        # y reescribimos. Caso real 2026-04-27: state.json tenia 482.46 viejo
        # y BotConfig pasaba 429.77 nuevo. _load pisaba el real con el viejo.
        # Trust the constructor: si BotConfig dice X, X es la verdad.
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            cached_equity = float(data.get("current_equity", 0.0))
            ctor_equity = float(self.current_equity)
            # Threshold 5%: tolerancia para drift normal de PnL en el dia.
            if ctor_equity > 0 and cached_equity > 0:
                drift_pct = abs(cached_equity - ctor_equity) / ctor_equity
                if drift_pct > 0.05:
                    # Constructor manda. Reescribimos state.json con valores frescos.
                    logger_msg = (
                        "RiskManager: state.json desfasado (cached=%.2f vs ctor=%.2f, "
                        "drift=%.1f%%). Ignorando cache, reescribiendo con capital real."
                    ) % (cached_equity, ctor_equity, drift_pct * 100)
                    try:
                        import logging as _lg
                        _lg.getLogger(__name__).warning(logger_msg)
                    except Exception:
                        pass
                    # NO cargamos nada del state.json viejo. Forzamos save con valores ctor.
                    self._save()
                    return
            # Drift razonable: cargamos el state.json normal.
            self.high_watermark = float(data.get("high_watermark", self.high_watermark))
            self.current_equity = float(data.get("current_equity", self.current_equity))
            self.daily_pnl = float(data.get("daily_pnl", 0.0))
            self.daily_anchor_equity = float(
                data.get("daily_anchor_equity", self.daily_anchor_equity)
            )
            # RM_INDENT_FIX_V1 — reindentado: estas 5 lineas estaban con 16 espacios.
            self.daily_anchor_ts = float(data.get("daily_anchor_ts", time.time()))
            self.halted = bool(data.get("halted", False))
            self.halt_reason = str(data.get("halt_reason", ""))
            logger.info("Estado cargado: HWM=%.2f equity=%.2f halted=%s",
                        self.high_watermark, self.current_equity, self.halted)
        except Exception as exc:
            logger.error("No se pudo leer %s: %s. Usando defaults.",
                         self.state_path, exc)

    def _save(self) -> None:
        try:
            self.state_path.write_text(json.dumps({
                "high_watermark": self.high_watermark,
                "current_equity": self.current_equity,
                "daily_pnl": self.daily_pnl,
                "daily_anchor_equity": self.daily_anchor_equity,
                "daily_anchor_ts": self.daily_anchor_ts,
                "halted": self.halted,
                "halt_reason": self.halt_reason,
                "updated_at": time.time(),
            }, indent=2))
        except Exception as exc:
            logger.error("No se pudo guardar estado: %s", exc)

    # ---------------------------------------------------------------- equity
    def update_equity(self, equity: float) -> None:
        self.current_equity = float(equity)
        if self.current_equity > self.high_watermark:
            self.high_watermark = self.current_equity

        if time.time() - self.daily_anchor_ts >= 24 * 3600:
            self.daily_anchor_equity = self.current_equity
            self.daily_anchor_ts = time.time()
            self.daily_pnl = 0.0
        else:
            self.daily_pnl = self.current_equity - self.daily_anchor_equity

        self._check_global_drawdown()
        self._save()

    # -------------------------------------------------------------- metricas
    @property
    def drawdown_pct(self) -> float:
        if self.high_watermark <= 0:
            return 0.0
        return max(0.0, (self.high_watermark - self.current_equity) / self.high_watermark)

    # ----------------------------------------------------------------- halts
    def _check_global_drawdown(self) -> None:
        if self.halted:
            return
        if self.drawdown_pct >= MAX_DRAWDOWN_PCT:
            reason = ("Drawdown global %.2f%% >= %.2f%% (HWM=%.2f, equity=%.2f)"
                      % (self.drawdown_pct * 100, MAX_DRAWDOWN_PCT * 100,
                         self.high_watermark, self.current_equity))
            self.trigger_shutdown(reason)

    def trigger_shutdown(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        logger.critical("SHUTDOWN activado: %s", reason)
        try:
            SHUTDOWN_FLAG_PATH.write_text(
                "%s - %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), reason)
            )
        except Exception as exc:
            logger.error("No se pudo escribir shutdown.flag: %s", exc)
        self._save()

    def is_halted(self) -> bool:
        if self.halted:
            return True
        if SHUTDOWN_FLAG_PATH.exists():
            self.halted = True
            self.halt_reason = self.halt_reason or "shutdown.flag presente"
            return True
        return False

    # ----------------------------------------------------- sizing / exposicion
    def max_position_size_usdc(self) -> float:
        # DYNAMIC_MAX_POSITION_PCT_V1: usamos el pct dinamico de BotConfig
        # si esta seteado, sino caemos al hardcoded MAX_POSITION_PCT.
        base = self.current_equity if self.current_equity > 0 else CAPITAL_USDC
        pct = getattr(self, "_dynamic_max_position_pct", None) or MAX_POSITION_PCT
        return base * pct

    def deployable_capital(self, currently_deployed: float) -> float:
        # Usar equity actual (live) en vez del hardcoded CAPITAL_USDC=30.
        base = self.current_equity if self.current_equity > 0 else CAPITAL_USDC
        usable = base * (1.0 - RESERVE_PCT)
        cap = min(usable, MAX_TOTAL_EXPOSURE_USDC)
        return max(0.0, cap - currently_deployed)

    def can_open_new_position(self, currently_deployed: float) -> bool:
        if self.is_halted():
            return False
        return self.deployable_capital(currently_deployed) >= self.max_position_size_usdc()

    # ---------------------------------------------------- perdidas por pos.
    def check_positions(self, positions: List[Dict[str, Any]]) -> List[str]:
        to_close = []
        for p in positions:
            pnl = float(p.get("unrealized_pnl", 0.0))
            if pnl <= -MAX_LOSS_PER_POSITION_USDC:
                mid = p.get("market_id") or p.get("market")
                logger.warning("Posicion %s supera perdida maxima: %.2f USDC",
                               mid, pnl)
                if mid:
                    to_close.append(mid)
        return to_close

    # -------------------------------------------------------- exposicion total
    def enforce_exposure_cap(self, currently_deployed: float) -> Tuple[bool, str]:
        if currently_deployed > MAX_TOTAL_EXPOSURE_USDC:
            msg = ("Exposicion %.2f supera cap %.2f"
                   % (currently_deployed, MAX_TOTAL_EXPOSURE_USDC))
            logger.error(msg)
            return False, msg
        return True, ""
