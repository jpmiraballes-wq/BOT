# Bot v3 - Fix agresivo de fills

## Problema identificado
El bot v2 pone `BUY limit` al mid - half_spread, dentro del bid-ask.
En Polymarket (CLOB ilíquido con capital chico), NADIE agrede esas
órdenes. Quedan pending 2 horas hasta que el stale canceller las mata.
Resultado: 0 fills, 0 PnL.

## Fix v3

### 1. order_manager.py - Orden agresiva
- Ahora hace **BUY al best_ask** (cruza el spread) → fill inmediato.
- Sacrificamos el edge del half_spread pero ganamos flujo real.
- stale timeout pasa de 2h → 10 min.

### 2. circuit_breakers.py - Skip tails
- Precios <0.15 o >0.85 → size_factor = 0 (antes 1/3).
- Doble seguridad: `filter_opportunity` tambien los filtra.

### 3. market_scanner.py - Filtros estrictos
- MIN_VOLUME: 10K → 50K.
- MIN_LIQUIDITY: 5K → 20K.
- mid_price <0.15 o >0.85 rechazado.

### 4. config_append.py - Thresholds
- EXTREME_LOW: 0.08 → 0.15
- EXTREME_HIGH: 0.92 → 0.85

## Instalacion en el servidor

```bash
cd ~
git clone https://github.com/jpmiraballes-wq/BOT.git bot-v3-repo || (cd bot-v3-repo && git pull)
cd bot-v3-repo/bot-v3-fix
cp order_manager.py circuit_breakers.py market_scanner.py ~/polymarket-bot/
cat config_append.py >> ~/polymarket-bot/config.py
sudo systemctl restart polymarket-bot
sudo journalctl -u polymarket-bot -f
```

## Que esperar despues

- Primer ciclo (20s): scanner devuelve menos mercados (filtros estrictos).
- Segundo ciclo: coloca BUY agresivo en top mercado → se llena en segundos.
- ~15 min despues: primer cierre (take_profit o stop_loss).
- Capital empieza a rotar: 5-10 trades/hora en vez de 1-2/dia.
