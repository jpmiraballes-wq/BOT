# AGENT_MEMORY — Opus Trading Bot

**Reglas duras y mapa del sistema. Leerlo ANTES de cualquier cambio.**
_Última actualización: 2026-04-23 (fix copy-trade executor)_

---

## 🚨 REGLAS DURAS (no negociables)

1. **YO manejo el código del bot Python via GitHub.** Estoy conectado al repo `jpmiraballes-wq/BOT` con scope `repo`. Si hace falta tocar Python, entro, leo, pusheo. **No le pregunto al usuario que abra VSCode ni que haga git pull manualmente.** El usuario solo hace `git pull && systemctl restart bot` (o equivalente) cuando yo le aviso que hay un push nuevo.

2. **NUNCA sugerir que el usuario corra código.** Si falta heartbeat, es problema de infra (Mac/Railway/Fly.io off). Si el código está mal, lo arreglo yo en el repo.

3. **Antes de cualquier cambio** abrir este archivo (`AGENT_MEMORY.md`) y el `ROADMAP.md`. Confirmar qué proceso hace qué antes de tocar.

4. **Minimum viable change.** Nada de refactors grandes. Un bug → un fix. Nada más.

5. **Nunca romper `pending_fill=true` desde Base44 functions.** Ese flag es territorio EXCLUSIVO del bot Python (`OrderManager.drain_pending_fills`). Cualquier automation que lea Positions tiene que skipear las `pending_fill=true`.

---

## 🗺️ MAPA DEL SISTEMA

### Qué corre dónde

| Componente              | Carpeta / Archivo                          | Ejecuta en            |
| ----------------------- | ------------------------------------------ | --------------------- |
| Bot Python (live)       | `bot-v2/main.py`                           | Mac/Railway/Fly.io    |
| Bot viejo (NO usar)     | `main.py` (raíz) + `bot/`                  | —                     |
| Scheduled automations   | `functions/*` (Deno Deploy)                | Base44                |
| Telegram webhook        | `functions/telegramWebhook`                | Base44                |
| App React (dashboard)   | `pages/*`, `components/*`                  | Base44 CDN            |

### Flujo copy-trade whale (el que nos rompió la cabeza)

1. `whaleDetectConsensus` (scheduled) → crea `CopyTradeProposal` + manda Telegram con botones.
2. Usuario toca ✅ $X en Telegram → `telegramWebhook` (callback_query) → ejecuta inline → crea `Position` con `pending_fill=true` y `strategy=whale_consensus`.
3. **Bot Python** (loop `bot-v2/main.py`, cada ~30s): `om.drain_pending_fills()` → lee Positions pending → coloca FAK en CLOB → actualiza Position (fill u closed con motivo real). **ESTE PASO ES EL QUE FALTABA HASTA 2026-04-23.**
4. `syncPositionsWithWallet` (scheduled, cada pocos min): compara Base44 vs on-chain. **SKIP si `pending_fill=true`**.
5. `paperTradeClose` / auto_close: monitorea TP/SL en Positions llenadas.

### Claves / secrets críticos

- `PRIVATE_KEY` / `WALLET_ADDRESS` / `CLOB_*` → solo en la Mac del usuario. Yo NO los toco.
- `EXTERNAL_BASE44_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` → en Base44 secrets.
- Wallet Polymarket: `0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb`.
- Base44 App ID: `69e1e225a40599eb44ced81e`.

---

## 🛠️ Cómo depurar cuando el usuario dice "no ejecuta"

1. `SystemState.last_heartbeat`: si >5min, el proceso Python está caído. Es infra. Si está fresco, el bot está vivo.
2. Ver qué `bot_version` reporta `SystemState` → confirma qué carpeta del repo está activa (`v2` = `bot-v2/`).
3. Leer ese `main.py` en GitHub vía `readBotFile/index`. **¿Importa y llama la función que debería correr?**
4. Si falta lógica → push quirúrgico al repo. El usuario hace git pull + restart.
5. NUNCA asumir que el problema es que "el usuario no corrió el bot". Confirmar heartbeat primero.

---

## 📜 Historia de bugs resueltos

- **2026-04-23 — Copy-trade jamás llega al CLOB.**
  Causa: `bot-v2/whale_consensus.py` es paper-only, `bot-v2/main.py` no llamaba `drain_pending_fills`, y el método ni existía en `bot-v2/order_manager.py` (estaba en `bot/order_manager.py`, carpeta huérfana). Además `syncPositionsWithWallet` mataba las Positions pending_fill por race condition.
  Fix: (1) guard `pending_fill=true` en `syncPositionsWithWallet`, (2) método `drain_pending_fills` añadido a `bot-v2/order_manager.py`, (3) llamada inyectada en `bot-v2/main.py` al inicio del loop. Redeploy via `pushCopyTradeExecutor`.

---

## 🍎 REGLA DURA — INSTRUCCIONES PARA LA MAC (paso a paso, siempre)

**Cuando pushee un cambio al repo, SIEMPRE darle al usuario el bloque exacto para copiar-pegar en la terminal de la Mac.** Nada de "hacé git pull y restart" vago. El usuario copia-pega y listo.

Formato obligatorio (copiar tal cual, ajustando ruta/nombre del proceso):

\`\`\`bash
# 1. En la ventana donde corre el bot: Ctrl+C
# 2. Después:
cd ~/BOT
git pull
cd bot-v2
python3 main.py
\`\`\`

Notas:
- El usuario corre el bot manualmente con `python3 main.py` dentro de `bot-v2/` (no hay systemd en su Mac).
- Si en algún momento se cambia a systemd/launchd, actualizar este bloque.
- NUNCA pedir logs ni screenshots si el usuario no los ofrece. Confiar en heartbeat + lectura de código en GitHub.
