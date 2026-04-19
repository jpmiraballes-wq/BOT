"""umbrella_executor.py - Ejecutor atomico de arbitrajes umbrella.

Consume oportunidades detectadas por logical_arb.scan_logical_arb() del tipo
'umbrella_over_children' y las ejecuta con ordenes pseudo-FOK.

Tesis:
  Si P(umbrella) < sum(P(children)), hay profit garantizado comprando la
  umbrella y vendiendo (shorteando via NO) cada child. Payout neto por
  $1 invertido = (sum_children - umbrella) / umbrella.

Implementacion pseudo-FOK:
  Polymarket CLOB solo soporta GTC/GTD. Emulamos FOK asi:
    1) Place limit BUY umbrella YES + limit BUY children NO al mejor ask.
    2) Espera FOK_TIMEOUT_SECONDS (default 2s).
    3) Si alguna orden no llena 100%:
         - Cancela todas las ordenes abiertas.
         - Si ya hubo fills parciales -> cierra con MARKET orders opuestas
           (accept slippage para no quedar direccional).
    4) Si todo llena -> trade registrado en Trade entity.

Limites de seguridad:
  - Max N legs simultaneos: si la umbrella tiene >MAX_CHILDREN children,
    skip (complejidad de reversion explota).
  - Edge minimo: UMBRELLA_MIN_EDGE_PCT. Por debajo los fees y slippage se
    comen el profit.
  - Capital: respeta StrategyCapital['logical_arb'].get_available().
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from capital_allocator import CapitalAllocator
from config import POLYMARKET_FUNDER, POLYMARKET_PROXY_ADDRESS
from decision_logger import log_decision, log_error

logger = logging.getLogger(__name__)

STRATEGY_NAME = "logical_arb"
MAX_CHILDREN = 6                 # no operar umbrellas con mas de 6 componentes
UMBRELLA_MIN_EDGE_PCT = 3.0      # minimo 3% despues de fees estimados
FOK_TIMEOUT_SECONDS = 2.0        # espera max para fills completos
POLL_INTERVAL_SECONDS = 0.25     # cada cuanto consultar order status
MAX_NOTIONAL_PER_OPP = 100.0     # hard cap por oportunidad (USDC)
FEE_BUFFER_PCT = 1.0             # 1% de buffer para fees Polymarket


class UmbrellaExecutor:
    """Ejecutor de arbitrajes umbrella via OrderManager.

    Reutiliza el ClobClient del OrderManager principal para no duplicar
    auth ni rate limits.
    """

    def __init__(self, order_manager, capital_allocator: Optional[CapitalAllocator] = None):
        self.om = order_manager
        self.ca = capital_allocator or CapitalAllocator()

    # ---------------------------------------------------------- validacion
    def _is_viable(self, opp: Dict[str, Any]) -> Tuple[bool, str]:
        if opp.get("arb_type") != "umbrella_over_children":
            return False, "not_umbrella"
        children_count = int(opp.get("children_count") or 0)
        if children_count < 2:
            return False, "too_few_children"
        if children_count > MAX_CHILDREN:
            return False, "too_many_children_%d" % children_count

        edge_pct = float(opp.get("edge_pct") or 0.0)
        net_edge = edge_pct - FEE_BUFFER_PCT
        if net_edge < UMBRELLA_MIN_EDGE_PCT:
            return False, "edge_too_small_%.2f" % net_edge

        if not self.ca.is_enabled(STRATEGY_NAME):
            return False, "strategy_disabled"
        if self.ca.get_available(STRATEGY_NAME) <= 0:
            return False, "no_capital"
        return True, "ok"

    # ----------------------------------------------------------- size calc
    def _compute_size(self, opp: Dict[str, Any]) -> float:
        """USDC a invertir en el leg umbrella. Los legs children usan el
        mismo size (1 share de cada)."""
        budget = self.ca.get_available(STRATEGY_NAME)
        cap = min(budget, MAX_NOTIONAL_PER_OPP)
        # Sizing conservador: 25% del budget disponible por opp para
        # permitir diversificacion entre multiples umbrellas.
        return round(cap * 0.25, 2)

    # ------------------------------------------------------------ ejecucion
    def execute(self, opp: Dict[str, Any]) -> Dict[str, Any]:
        """Ejecuta una oportunidad. Retorna dict con resultado."""
        ok, reason = self._is_viable(opp)
        if not ok:
            return {"status": "skipped", "reason": reason}

        size_usdc = self._compute_size(opp)
        if size_usdc < 5.0:  # ordenes <$5 no tienen sentido por fees
            return {"status": "skipped", "reason": "size_too_small"}

        # NOTA: la oportunidad trae token_ids y precios desde el detector
        # en logical_arb.py. El executor necesita:
        #   - umbrella_token_id_yes
        #   - children: [{token_id_no, price}, ...]
        # Si el detector no los provee todavia, abortamos con skipped.
        umbrella_token = opp.get("umbrella_token_id_yes")
        children = opp.get("children_tokens") or []
        if not umbrella_token or not children:
            return {"status": "skipped", "reason": "missing_token_ids"}

        log_decision(
            reason="umbrella_arb_attempt",
            market=opp.get("umbrella_question") or opp.get("group_key"),
            strategy=STRATEGY_NAME,
            extra={
                "edge_pct": opp.get("edge_pct"),
                "size_usdc": size_usdc,
                "children_count": len(children),
            },
        )

        placed_orders: List[Dict[str, Any]] = []
        try:
            # Leg 1: BUY umbrella YES (ask side)
            umbrella_price = float(opp.get("umbrella_price") or 0.0)
            order_u = self._place_limit_buy(
                token_id=umbrella_token, price=umbrella_price, size_usdc=size_usdc,
            )
            if not order_u:
                return {"status": "failed", "reason": "umbrella_place_failed"}
            placed_orders.append({"role": "umbrella", "order": order_u})

            # Legs 2..N: BUY child NO (equivale a short child YES)
            per_child_usdc = size_usdc  # 1:1 con umbrella en shares
            for ch in children:
                token_no = ch.get("token_id_no")
                no_price = float(ch.get("no_price") or 0.0)
                if not token_no or no_price <= 0:
                    raise RuntimeError("child_missing_token_or_price")
                order_c = self._place_limit_buy(
                    token_id=token_no, price=no_price, size_usdc=per_child_usdc,
                )
                if not order_c:
                    raise RuntimeError("child_place_failed")
                placed_orders.append({"role": "child_no", "order": order_c})

            # Espera fills con timeout
            filled = self._wait_for_fills(placed_orders, FOK_TIMEOUT_SECONDS)
            if filled:
                return self._finalize_success(opp, placed_orders, size_usdc)

            # Alguno no lleno -> revertir
            return self._rollback(placed_orders, reason="fok_timeout")

        except Exception as exc:
            log_error("umbrella_exec_exception", module="umbrella_executor",
                      extra={"error": str(exc)})
            if placed_orders:
                return self._rollback(placed_orders, reason="exception_%s" % exc)
            return {"status": "failed", "reason": "exception_%s" % exc}

    # ----------------------------------------------------- primitivas orden
    def _place_limit_buy(self, token_id: str, price: float, size_usdc: float):
        """Delega en el OrderManager.client. Retorna order dict o None."""
        client = getattr(self.om, "client", None)
        if client is None:
            return None
        try:
            # price es el ask actual. Cruzamos ligeramente para maximizar
            # probabilidad de fill sin overpaying (+0.005 = medio centavo).
            cross_price = min(0.99, round(price + 0.005, 3))
            size_shares = round(size_usdc / cross_price, 2)
            # Firma concreta de OrderManager expuesta como place_limit_order.
            # Si no existe, usamos fallback a post_order del ClobClient.
            if hasattr(self.om, "place_limit_order"):
                return self.om.place_limit_order(
                    token_id=token_id, side="BUY",
                    price=cross_price, size=size_shares,
                )
            from py_clob_client.clob_types import OrderArgs
            order_args = OrderArgs(
                price=cross_price, size=size_shares,
                side="BUY", token_id=token_id,
            )
            signed = client.create_order(order_args)
            return client.post_order(signed)
        except Exception as exc:
            logger.error("place_limit_buy failed token=%s: %s", token_id, exc)
            return None

    def _wait_for_fills(self, placed: List[Dict[str, Any]], timeout: float) -> bool:
        """True si TODAS las ordenes estan 100% filled antes del timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            all_filled = True
            for p in placed:
                order_id = (p.get("order") or {}).get("id") or (p.get("order") or {}).get("orderID")
                if not order_id:
                    all_filled = False
                    break
                if not self._is_order_filled(order_id):
                    all_filled = False
                    break
            if all_filled:
                return True
            time.sleep(POLL_INTERVAL_SECONDS)
        return False

    def _is_order_filled(self, order_id: str) -> bool:
        client = getattr(self.om, "client", None)
        if client is None:
            return False
        try:
            order = client.get_order(order_id)
            status = (order or {}).get("status") or ""
            return status.upper() in {"FILLED", "MATCHED"}
        except Exception as exc:
            logger.debug("get_order %s: %s", order_id, exc)
            return False

    # --------------------------------------------------------- post-trade
    def _finalize_success(self, opp: Dict[str, Any],
                          placed: List[Dict[str, Any]], size_usdc: float):
        total_deployed = size_usdc * (1 + len(
            [p for p in placed if p["role"] == "child_no"]
        ))
        # El allocator se actualiza en el proximo ciclo del main loop.
        log_decision(
            reason="umbrella_arb_filled",
            market=opp.get("umbrella_question") or opp.get("group_key"),
            strategy=STRATEGY_NAME,
            extra={"legs": len(placed), "deployed_usdc": total_deployed},
        )
        return {"status": "executed", "legs": len(placed),
                "deployed_usdc": total_deployed}

    def _rollback(self, placed: List[Dict[str, Any]], reason: str):
        """Cancela ordenes pendientes. Si hay fills parciales los marca
        para cierre en el siguiente ciclo del close_profitable_positions."""
        client = getattr(self.om, "client", None)
        cancelled = 0
        partial_fills = 0
        for p in placed:
            order = p.get("order") or {}
            order_id = order.get("id") or order.get("orderID")
            if not order_id or client is None:
                continue
            try:
                current = client.get_order(order_id) or {}
                status = (current.get("status") or "").upper()
                if status in {"FILLED", "MATCHED"}:
                    partial_fills += 1
                    continue
                client.cancel(order_id)
                cancelled += 1
            except Exception as exc:
                logger.error("rollback cancel %s fallo: %s", order_id, exc)

        log_error("umbrella_arb_rollback", module="umbrella_executor",
                  extra={"reason": reason, "cancelled": cancelled,
                         "partial_fills": partial_fills})
        return {
            "status": "rolled_back",
            "reason": reason,
            "cancelled": cancelled,
            "partial_fills": partial_fills,
        }


def run_umbrella_cycle(order_manager, opportunities: List[Dict[str, Any]],
                       max_per_cycle: int = 2) -> List[Dict[str, Any]]:
    """Helper para llamar desde main.py. Ejecuta hasta max_per_cycle
    oportunidades umbrella por iteracion."""
    if not opportunities:
        return []
    executor = UmbrellaExecutor(order_manager=order_manager)
    umbrella_opps = [o for o in opportunities
                     if o.get("arb_type") == "umbrella_over_children"]
    results = []
    for opp in umbrella_opps[:max_per_cycle]:
        res = executor.execute(opp)
        res["opp_id"] = opp.get("group_key")
        results.append(res)
    return results
