# Independent Odds Engine V1

Motor independiente para Polymarket basado en Odds API, datos publicos de Polymarket y paper trading.

Principios:
- No copy trading.
- No whales.
- No executor real en esta version.
- Primero OBSERVE y PAPER.
- Cada senal debe tener mapping, freshness, edge y risk result.

Flujo:
1. Lee eventos de The Odds API.
2. Lee eventos/mercados activos de Polymarket Gamma API.
3. Mapea eventos con score de confianza.
4. Calcula fair value desde odds normalizadas.
5. Lee precios publicos de Polymarket.
6. Genera senales si hay edge.
7. Pasa por risk manager.
8. Simula paper trades.
9. Guarda logs locales JSONL y opcionalmente reporta a Base44.

Instalacion:
cd odds_engine
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run_once.py
python main.py

Modos:
BOT_MODE=OBSERVE guarda datos sin crear paper trades.
BOT_MODE=PAPER crea senales y paper trades simulados.

Variables minimas:
ODDS_API_KEY
ODDS_SPORT_KEYS=mma_mixed_martial_arts,soccer_uefa_champs_league
