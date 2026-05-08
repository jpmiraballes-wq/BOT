# JP Test Steps — Independent Odds Engine V1

## Estado actual

Esta rama contiene un motor nuevo en `odds_engine/`. No toca `bot-v2/` y no ejecuta ordenes reales.

## Paso 1 — Pull de la rama

```bash
cd ~/BOT
git fetch origin
git checkout independent-odds-engine-v1
git pull origin independent-odds-engine-v1
```

## Paso 2 — Crear entorno aislado

```bash
cd ~/BOT/odds_engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example.txt .env
```

## Paso 3 — Editar .env

Agregar:

```bash
ODDS_API_KEY=TU_CLAVE_DE_THE_ODDS_API
EXTERNAL_BASE44_API_KEY=TU_CLAVE_BASE44
BASE44_APP_ID=69e1e225a40599eb44ced81e
BASE44_WRITE_ENABLED=true
BOT_MODE=PAPER
```

Para primera prueba ultra segura, podés usar:

```bash
BASE44_WRITE_ENABLED=false
BOT_MODE=OBSERVE
```

## Paso 4 — Primera ejecución sin loop

```bash
python run_once.py
```

## Paso 5 — Revisar archivos locales

```bash
ls -lah data
cat data/bot_logs.jsonl | tail -20
cat data/signal.jsonl | tail -10
cat data/papertrade.jsonl | tail -10
```

## Paso 6 — Si falla

Copiar y pegar en ChatGPT:

- el error completo de terminal
- las ultimas 20 lineas de `data/bot_logs.jsonl`
- el contenido de `.env` SIN claves privadas ni API keys visibles

## Regla de seguridad

No ejecutar `bot-v2/main.py` para este test. El motor nuevo vive en `odds_engine/`.
