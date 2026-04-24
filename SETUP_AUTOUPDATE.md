# 🤖 Auto-update del bot (AUTO_UPDATE_V1)

Este setup hace que **nunca tengas que reiniciar el bot a mano**.

## Qué hace

- **run_bot.sh** — wrapper infinito. Si main.py crashea, lo relanza en 10s.
- **auto_updater.py** — cada 15min el bot hace `git fetch` + `git pull` si hay commits nuevos. Al detectar update → `sys.exit(0)` → wrapper lo relanza con código fresco.
- **com.jp.polybot.plist** — launchd de macOS. Arranca el wrapper al bootear el Mac y lo mantiene vivo.

## Instalación one-shot (solo la primera vez)

```bash
# 1) Pulleá el código nuevo
cd ~/BOT
git pull origin main

# 2) Dale permisos al wrapper
chmod +x bot-v2/run_bot.sh

# 3) Matá cualquier instancia manual anterior
pkill -f "python.*main.py" 2>/dev/null
sleep 2

# 4) Copiá el plist a LaunchAgents reemplazando tu HOME
mkdir -p ~/Library/LaunchAgents
sed "s|HOME_DIR_PLACEHOLDER|$HOME|g" bot-v2/com.jp.polybot.plist > ~/Library/LaunchAgents/com.jp.polybot.plist

# 5) Cargá el servicio
launchctl unload ~/Library/LaunchAgents/com.jp.polybot.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.jp.polybot.plist

# 6) Verificá que arrancó
sleep 3
tail -f ~/bot.log
```

Si ves `[run_bot ...] starting main.py...` seguido de logs del bot → **listo**. Ctrl+C para salir del tail, el bot sigue vivo.

## Uso diario (ya instalado)

**No tenés que hacer nada.** Vos pusheás desde Base44 → 15min después el bot corre código nuevo solo.

## Comandos útiles

```bash
# Ver logs en vivo
tail -f ~/bot.log

# Forzar reinicio ya (sin esperar 15min)
launchctl kickstart -k gui/$(id -u)/com.jp.polybot

# Parar el bot del todo
launchctl unload ~/Library/LaunchAgents/com.jp.polybot.plist

# Volver a arrancarlo
launchctl load ~/Library/LaunchAgents/com.jp.polybot.plist

# Ver si está corriendo
launchctl list | grep polybot
```

## Troubleshooting

- **"No veo nada en bot.log"** → mirá `~/bot_launchd.err.log` (errores del wrapper antes de arrancar python).
- **"El bot no se actualiza"** → comprobá que `git status` en ~/BOT está limpio (no hay cambios locales que bloqueen el `pull --ff-only`).
- **"Cambié de branch"** → setear env var `BOT_BRANCH` en el plist (default: main).

---

_Última actualización: 2026-04-24 — AUTO_UPDATE_V1_
