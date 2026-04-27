# 🚨 REPORTER ZOMBIE BUG — INVESTIGACIÓN PARA OPUS

**Fecha:** 2026-04-27
**Reporta:** Agente cloud (Bolt) + JP

## Síntoma

`SystemState` está siendo escrito con un patrón alternante:

| Minuto UTC | open_positions | total_pnl | capital_deployed | total_trades | win_rate |
|---|---|---|---|---|---|
| 09:40:44 | 5 | -7.99 | 143.10 | 9 | 33.33 |
| 09:45:48 | **0** ❌ | **0** ❌ | **0** ❌ | **0** ❌ | **0** ❌ |
| 09:50:48 | 5 | -7.99 | 143.10 | 9 | 33.33 |
| 09:55:51 | **0** ❌ | **0** ❌ | **0** ❌ | **0** ❌ | **0** ❌ |
| 10:00:52 | 5 | -7.99 | 143.10 | 9 | 33.33 |

Cada ~5min llega un escritor "zombie" que pisa todo con ceros. El dashboard
(que toma SystemState el más reciente) muestra ceros la mitad del tiempo.

## created_by_id

Todos los registros vienen del mismo `created_by_id` (la cuenta de JP).
Eso descarta dos servicios distintos. La causa es:

1. **Hipótesis A:** Hay 2 procesos `python main.py` corriendo en la Mac.
   Uno bien configurado, otro que arrancó sin contexto y reporta vacío.
2. **Hipótesis B:** El reporter tiene un bug de race condition: arranca el
   thread del heartbeat antes de que el primer ciclo de `portfolio_sync`
   haya popado las posiciones, y escribe SystemState con valores iniciales (0).
3. **Hipótesis C:** launchd está relanzando el proceso cada cierto tiempo
   y queda un proceso "viejo" que no fue killeado del todo.

## Diagnóstico desde la Mac

```bash
# 1) Ver cuántos procesos main.py hay vivos
ps aux | grep main.py | grep -v grep

# Si hay MÁS DE UNO → matar todos menos el más reciente:
# kill -9 <PID_VIEJO>

# 2) Ver el reporter en acción (los logs deben mostrar la fuente del 0)
tail -f bot.log | grep -i "heartbeat\|reporter\|systemstate"

# 3) Verificar el código del reporter
grep -n "open_positions" bot-v2/reporter.py
grep -n "SystemState" bot-v2/reporter.py

# 4) Si hay 2 schedulers internos, buscar:
grep -n "schedule\|threading.Timer\|Thread" bot-v2/reporter.py
grep -n "schedule\|threading.Timer\|Thread" bot-v2/main.py
```

## Fix esperado

- Si es hipótesis A → matar duplicados, dejar UNO solo. Verificar launchd plist.
- Si es hipótesis B → en `reporter.py`, antes de escribir SystemState,
  validar que `open_positions > 0` O que `total_trades > 0` antes de
  considerarlo "estado válido". Si todos los campos son 0 al mismo tiempo
  y el bot lleva >1min activo, NO escribir (o escribir con `mode=warming_up`).
- Si es hipótesis C → revisar launchd `com.jp.polybot.plist` y agregar
  `KeepAlive` con `SuccessfulExit: false`.

## Workaround mientras tanto

Hay una función cloud `polymarketUpdatePositionPnl` que cada 5min escribe
`current_price` y `pnl_unrealized` en cada Position abierta. El dashboard
ahora deriva el PnL real desde Position, no desde SystemState.

Cuando Opus arregle el reporter:
1. Verificar que `SystemState` deja de tener registros con ceros.
2. Considerar si seguir con `polymarketUpdatePositionPnl` o no
   (es nice-to-have de todas formas para que el frontend tenga datos
   sin depender del bot Mac).

— Bolt+Opus+JP coordinación
