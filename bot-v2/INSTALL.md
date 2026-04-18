# Bot v2 - Instrucciones de instalacion

## Pasos

1. Backup:
   ```bash
   cp -r ~/polymarket-bot ~/polymarket-bot.bak
   ```

2. Descargar del repo:
   ```bash
   cd ~
   git clone https://github.com/jpmiraballes-wq/BOT.git bot-v2-repo
   cd bot-v2-repo/bot-v2
   ```

3. Copiar los .py al bot:
   ```bash
   cp main.py base44_client.py decision_logger.py kelly.py \
      logical_arb.py circuit_breakers.py order_manager.py \
      ~/polymarket-bot/
   ```

4. Ampliar config.py (no sobreescribir):
   ```bash
   cat config_append.py >> ~/polymarket-bot/config.py
   ```

5. Aplicar patch a market_scanner.py:
   ```bash
   cd ~/polymarket-bot
   patch -p0 < ~/bot-v2-repo/bot-v2/market_scanner.patch
   ```

6. Reiniciar:
   ```bash
   sudo systemctl restart polymarket-bot
   sudo journalctl -u polymarket-bot -f
   ```

Deberias ver: `Polymarket Market Maker v2 - arrancando`.
