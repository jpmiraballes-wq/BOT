# Independent Odds Engine V1

Motor independiente para Polymarket basado en Odds API, datos publicos de Polymarket y paper trading.

## Principios

- No copy trading.
- No whales.
- No executor real en esta version.
- No private key ni wallet.
- Primero ingesta segura, despues PAPER.
- Cada senal debe tener mapping, freshness, edge y risk result.

## Arquitectura

Python ejecuta el motor. Base44 es dashboard/config/auditoria. NodeZap queda fuera del motor de decision.

## Flujo

1. Lee BotConfig desde Base44.
2. Lee eventos de The Odds API.
3. Lee mercados activos de Polymarket Gamma API.
4. Mapea eventos con score de confianza.
5. Calcula fair value desde odds normalizadas.
6. Lee precios publicos de Polymarket.
7. Guarda ExternalEvent, OddsSnapshot, PolymarketEvent, PolymarketMarket, PolymarketSnapshot, EventMapping y MarketMapping.
8. Si BotConfig.enabled=false: termina ahi, sin senales ni paper trades.
9. Si BotConfig.enabled=true y mode=paper: genera Signal y PaperTrade.
10. Guarda auditoria local JSONL completa y envia muestra controlada a Base44.

## Instalacion

```bash
cd odds_engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example.txt .env
python run_once.py
```

## Primer modo seguro

En Base44 dejar:

- enabled=false
- mode=paper

Con eso el bot solo ingesta datos y llena dashboard sin crear senales ni trades.

## Segundo modo, paper

Cuando la ingesta este validada, cambiar en Base44:

- enabled=true
- mode=paper

Esto crea Signal y PaperTrade. Sigue sin tocar dinero real.

## Variables minimas

- ODDS_API_KEY
- EXTERNAL_BASE44_API_KEY
- BASE44_APP_ID=69e1e225a40599eb44ced81e
- ODDS_SPORT_KEYS=mma_mixed_martial_arts,soccer_uefa_champs_league

## Throttle Base44

Local JSONL guarda todo. Base44 recibe una muestra controlada para no inundar el dashboard:

- BASE44_MAX_EVENTS=60
- BASE44_MAX_ODDS_SNAPSHOTS=120
- BASE44_MAX_POLYMARKET_MARKETS=80
- BASE44_MAX_MAPPINGS=150

## Seguridad

Live mode esta bloqueado en V1. Si Base44 dice mode=live, Python lo fuerza internamente a PAPER.
