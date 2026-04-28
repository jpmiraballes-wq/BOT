"""_clob_compat.py — Capa de compatibilidad py-clob-client V1 → V2.

Permite que el código existente que usa la API V1
    (`from py_clob_client.client import ClobClient`,
     `client.create_order(args)` + `client.post_order(signed, OrderType.GTC)`,
     `from py_clob_client.order_builder.constants import BUY, SELL`)
siga funcionando contra el SDK V2 (`py_clob_client_v2`) tras el cutover de
Polymarket del 2026-04-28.

Diseño:
- Re-exportamos los símbolos comunes (`OrderArgs`, `OrderType`,
  `PartialCreateOrderOptions`, `MarketOrderArgs`, `ApiCreds`).
- `BUY`/`SELL` se exponen como strings (`"BUY"`/`"SELL"`) que V2 acepta
  vía el enum `Side` (Side.BUY.value == "BUY").
- `ClobClient` se hereda y se le agregan los métodos legacy
  `create_order(args, options=None)` y `post_order(signed, order_type=GTC)`.
  Internamente `create_order` retorna un dict-buffer con los args + options;
  `post_order` lo despacha a `create_and_post_order` de V2.
- `get_order_book` mantiene la firma V1.

NO modifica V2 globalmente. Cada archivo que antes hacía
    `from py_clob_client.* import ...`
ahora hace
    `from _clob_compat import ...`
y el resto del código no se toca.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Imports V2 reales ────────────────────────────────────────────
from py_clob_client_v2 import (
    ClobClient as _V2ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    ApiCreds,
)

try:
    from py_clob_client_v2 import MarketOrderArgs  # type: ignore
except ImportError:
    MarketOrderArgs = None  # algunos call-sites pueden no usarlo

try:
    from py_clob_client_v2 import BalanceAllowanceParams, AssetType  # type: ignore
except ImportError:
    BalanceAllowanceParams = None
    AssetType = None

# ── Constantes V1 (BUY/SELL como strings, V2.Side las acepta vía .value) ─
BUY = "BUY"
SELL = "SELL"


def _coerce_side(side: Any) -> Side:
    """Acepta string 'BUY'/'SELL' o ya un Side y devuelve Side."""
    if isinstance(side, Side):
        return side
    s = str(side).upper()
    if s == "BUY":
        return Side.BUY
    if s == "SELL":
        return Side.SELL
    raise ValueError(f"Invalid side: {side!r}")


def _normalize_order_args(args: Any) -> OrderArgs:
    """Si el call-site construyó OrderArgs(side=BUY) (string), V2 espera Side.
    Devolvemos OrderArgs con side ya coercionado.
    """
    # Si ya es un OrderArgs V2 válido, intentamos arreglar el side si vino como string.
    try:
        side = getattr(args, "side", None)
        if side is not None and not isinstance(side, Side):
            args.side = _coerce_side(side)
    except Exception:
        pass
    return args


class ClobClient(_V2ClobClient):
    """Sub-clase V2 con la API legacy de V1.

    create_order(args, options=None) → SignedOrder (firma inmediata, igual que V1)
    post_order(signed, order_type=GTC, **kw) → dict respuesta CLOB (passthrough V2)

    Diseño:
    - V1 firmaba en create_order y enviaba en post_order. Replicamos eso
      llamando al create_order REAL de V2 (que firma y retorna SignedOrder).
      Si el call-site pasó OrderArgs con side="BUY" string, lo coercionamos
      antes a Side.BUY.
    - post_order es un passthrough directo al post_order de V2, que ya acepta
      la firma (signed, order_type=GTC, post_only=False, defer_exec=False).
      Si por error nos pasan un OrderArgs sin firmar, lo firmamos al vuelo.
    """

    def create_order(self, order_args: OrderArgs,
                     options: Optional[PartialCreateOrderOptions] = None):
        # super().create_order firma y devuelve un SignedOrder V2.
        return super().create_order(_normalize_order_args(order_args), options)

    def post_order(self, order, order_type: OrderType = OrderType.GTC,
                   *args, **kwargs):
        # Defensive: si nos pasan OrderArgs sin firmar (bug de un call-site),
        # firmamos antes de despachar.
        if isinstance(order, OrderArgs):
            order = super().create_order(_normalize_order_args(order), None)
        return super().post_order(order, order_type, *args, **kwargs)


# ── V1 → V2 method aliases ─────────────────────────────────────
    def get_orders(self, *args, **kwargs):
        """V1 alias: client.get_orders() → V2 client.get_open_orders()."""
        return self.get_open_orders(*args, **kwargs)

    def cancel(self, order_id: str = None, **kwargs):
        """V1 alias: client.cancel(order_id=X) → V2 client.cancel_order(OrderPayload(orderID=X))."""
        oid = order_id or kwargs.get("orderID") or kwargs.get("order_id")
        if not oid:
            raise ValueError("cancel() requires order_id")
        try:
            from py_clob_client_v2 import OrderPayload  # type: ignore
            return self.cancel_order(OrderPayload(orderID=oid))
        except ImportError:
            # Fallback: si OrderPayload no se exporta, pasamos un objeto duck-typed.
            class _P:
                pass
            p = _P()
            p.orderID = oid
            return self.cancel_order(p)


__all__ = [
    "ClobClient",
    "OrderArgs",
    "OrderType",
    "PartialCreateOrderOptions",
    "MarketOrderArgs",
    "ApiCreds",
    "Side",
    "BUY",
    "SELL",
    "BalanceAllowanceParams",
    "AssetType",
]
