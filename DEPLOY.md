# Deploy del bot Polymarket 24/7

Este bot (`bot-v2/`) está pensado para correr 24/7 en un VPS cloud.
Dos opciones recomendadas: **Railway** (más fácil) o **Fly.io** (más robusto).

---

## Variables de entorno necesarias

El bot lee estas variables al arrancar. Configurálas en el dashboard del servicio cloud (NO las subas al repo).

### Obligatorias

| Variable | Descripción |
|---|---|
| `WALLET_ADDRESS` | Dirección EOA con la private key (ej. `0xAbc...`) |
| `PRIVATE_KEY` | Private key de la wallet (sin `0x` prefix, hex 64 chars) |
| `BASE44_API_KEY` | API key de Base44 para reportar al dashboard |
| `TELEGRAM_BOT_TOKEN` | Bot token para notificaciones (ya lo tenés) |
| `TELEGRAM_CHAT_ID` | Chat ID donde recibís alertas |

### Opcionales (tienen defaults)

| Variable | Default | Qué hace |
|---|---|---|
| `POLYMARKET_FUNDER` | `0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb` | Proxy/Safe de Polymarket |
| `POLYMARKET_PROXY_ADDRESS` | = `WALLET_ADDRESS` | Safe address para firmar |
| `POLYMARKET_SIGNATURE_TYPE` | `2` | 0=EOA, 1=Magic, 2=Safe |
| `DRY_RUN` | `false` | Si `true`, no ejecuta órdenes reales |
| `PAPER_CAPITAL_USDC` | `2000` | Capital paper trading |

---

## Opción 1 — Railway (recomendado, más fácil)

### Paso 1: Crear cuenta
1. Andá a https://railway.app
2. Login con GitHub (mismo user que tiene el repo `jpmiraballes-wq/BOT`)

### Paso 2: Crear proyecto
1. Click **New Project** → **Deploy from GitHub repo**
2. Elegí el repo `BOT`
3. Railway detecta el `Dockerfile` y el `railway.json` automáticamente
4. Click **Deploy** (la primera build tarda ~4 min)

### Paso 3: Configurar variables de entorno
En el proyecto → tab **Variables** → **Add variable** (una por una):

```
WALLET_ADDRESS=0x...           # tu EOA
PRIVATE_KEY=abc123...          # SIN 0x prefix
BASE44_API_KEY=...             # pedila en el dashboard de Base44
TELEGRAM_BOT_TOKEN=8632303483:AAE3lcHGX1dIf1MmIs996fgFz7XzpmN29Cg
TELEGRAM_CHAT_ID=...           # tu chat ID
POLYMARKET_FUNDER=0x7c6a42cb6ae0d63a7073eefc1a5e04f102facbfb
POLYMARKET_PROXY_ADDRESS=0x... # igual que WALLET_ADDRESS por default
POLYMARKET_SIGNATURE_TYPE=2
```

### Paso 4: Redeploy y verificar logs
1. Después de agregar las vars, Railway hace redeploy automático
2. Tab **Deployments** → click en el último deploy → **View Logs**
3. Deberías ver: `Polymarket Market Maker v2 — arrancando`
4. En el dashboard de Base44 → HEARTBEAT debería estar "fresh" (< 1min)

### Costo Railway
- **Hobby plan: $5/mes** incluye $5 de uso (suele alcanzar para un worker ligero)
- Si se pasa → $0.000463/GB-segundo de RAM

---

## Opción 2 — Fly.io (más robusto, preferido por algunos)

### Paso 1: Instalar flyctl local
```bash
# Mac
brew install flyctl

# Login
flyctl auth login
```

### Paso 2: Crear la app
Desde la raíz del repo local (git pull primero para tener Dockerfile + fly.toml):

```bash
cd BOT
flyctl launch --no-deploy
# Cuando pregunte:
#   - App name: polymarket-bot (o el que quieras)
#   - Organization: personal
#   - Region: mad (Madrid, más cerca de tu zona)
#   - Postgres: NO
#   - Redis: NO
#   - Deploy now: NO
```

### Paso 3: Setear secrets
```bash
flyctl secrets set \
  WALLET_ADDRESS=0x... \
  PRIVATE_KEY=abc... \
  BASE44_API_KEY=... \
  TELEGRAM_BOT_TOKEN=8632303483:AAE3lcHGX1dIf1MmIs996fgFz7XzpmN29Cg \
  TELEGRAM_CHAT_ID=... \
  POLYMARKET_SIGNATURE_TYPE=2 \
  POLYMARKET_PROXY_ADDRESS=0x...
```

### Paso 4: Deploy
```bash
flyctl deploy
```

Primera build tarda ~5 min. Después de deployar:
```bash
flyctl logs       # ver logs en vivo
flyctl status     # ver estado de la máquina
flyctl ssh console  # entrar al contenedor si hace falta debuggear
```

### Costo Fly.io
- **Free tier: 3 shared-cpu-1x VMs con 256MB RAM** — el bot entra justo en 512MB (tenés que upgradear)
- Con 512MB: ~$2/mes

---

## Después del deploy — Apagar el bot local

Una vez que veas en el dashboard de Base44 que HEARTBEAT está fresh desde el VPS:

1. En tu Mac, **parar el bot local**:
   ```bash
   # si usás launchd
   launchctl unload ~/Library/LaunchAgents/com.polymarket.bot.plist

   # o si lo corrés manualmente
   pkill -f "python.*main.py"
   ```
2. **Importante**: solo tiene que haber UN bot corriendo a la vez. Si corren dos, van a competir por las mismas proposals approved y duplicar órdenes.

---

## Troubleshooting

### "No veo heartbeats en Base44"
- Chequeá los logs del servicio cloud — ¿arrancó el bot?
- ¿Las env vars están bien seteadas? (`BASE44_API_KEY` sobre todo)

### "Las órdenes no se ejecutan"
- Chequeá logs del bot: busca `drain_pending_fills`
- ¿La wallet tiene USDC suficiente?
- ¿La `POLYMARKET_SIGNATURE_TYPE` es correcta? (2 = Safe, que es lo más común)

### "El bot se reinicia cada rato"
- Chequeá memoria: 512MB debería alcanzar. Si crashea con OOM, subí a 1GB.
- Railway: Settings → Resources → RAM
- Fly: `flyctl scale memory 1024`

---

## Cost summary

| Servicio | Costo mensual | Dificultad |
|---|---|---|
| Railway | ~$5 | ⭐ Fácil |
| Fly.io | ~$2 | ⭐⭐ Requiere CLI |
| Mac 24/7 encendida | $15 electricidad + ruido | ⭐ Fácil pero malo |

Recomendación: **Railway** para arrancar. Si después querés optimizar, migrás a Fly.io.
