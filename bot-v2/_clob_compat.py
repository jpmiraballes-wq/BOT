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


class _PendingOrder:
    """Buffer que devuelve create_order(); post_order() lo despacha."""
    __slots__ = ("args", "options")

    def __init__(self, args: OrderArgs, options: Optional[PartialCreateOrderOptions] = None):
        self.args = args
        self.options = options


class ClobClient(_V2ClobClient):
    """Sub-clase V2 con la API legacy de V1.

    create_order(args, options=None) → _PendingOrder (no firma todavía)
    post_order(_PendingOrder, order_type=OrderType.GTC) → dict respuesta CLOB

    Esto es semánticamente equivalente al flujo V1
    (V1 firmaba en create_order y enviaba en post_order; V2 hace ambas en
    create_and_post_order). El shim difiere la firma hasta post_order para
    mantener la firma de los call-sites existentes intactos.
    """

    def create_order(self, order_args: OrderArgs,
                     options: Optional[PartialCreateOrderOptions] = None) -> _PendingOrder:
        return _PendingOrder(_normalize_order_args(order_args), options)

    def post_order(self, pending: _PendingOrder,
                   order_type: OrderType = OrderType.GTC):
        if not isinstance(pending, _PendingOrder):
            # Por si algún call-site pasa directamente OrderArgs (defensive).
            pending = _PendingOrder(_normalize_order_args(pending), None)
        return self.create_and_post_order(
            order_args=pending.args,
            options=pending.options,
            order_type=order_type,
        )


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
