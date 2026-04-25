#!/usr/bin/env python3
"""
Backtest Scalping - RSI(14)/EMA(50) en BTCUSDT y ETHUSDT, 15m, ultimos 6 meses.

Logica:
- RSI cruza < 30  -> abrir LONG
- RSI cruza > 70  -> abrir SHORT
- TP: +1.5% / SL: -0.6%
- Size $150 por trade, max 3 posiciones concurrentes
- Sin fees ni slippage (analisis puro de la senal)

Uso:
    pip install pandas numpy requests
    python backtest_scalping.py
"""

import time
import math
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ---------------- Config ----------------
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "15m"
MONTHS = 6
RSI_PERIOD = 14
EMA_PERIOD = 50
RSI_LOW = 30
RSI_HIGH = 70
TP_PCT = 0.015
SL_PCT = 0.006
SIZE_USDC = 150.0
MAX_CONCURRENT = 3

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
LIMIT_PER_REQ = 1000


# ---------------- Data fetch ----------------
def fetch_klines(symbol, start_ms, end_ms):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": LIMIT_PER_REQ,
        }
        r = requests.get(BINANCE_KLINES, params=params, timeout=15)
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        rows.extend(chunk)
        last_open = chunk[-1][0]
        next_cursor = last_open + 15 * 60 * 1000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(chunk) < LIMIT_PER_REQ:
            break
        time.sleep(0.15)
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "trades", "tb_base", "tb_quote", "ignore"
    ])
    if df.empty:
        return df
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates(subset="open_time").reset_index(drop=True)
    return df[["open_time", "open", "high", "low", "close", "volume"]]


# ---------------- Indicators ----------------
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def ema(series, period=50):
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


# ---------------- Backtest ----------------
def backtest(df, symbol):
    df = df.copy()
    df["rsi"] = rsi(df["close"], RSI_PERIOD)
    df["ema"] = ema(df["close"], EMA_PERIOD)
    df["rsi_prev"] = df["rsi"].shift(1)

    trades = []
    open_positions = []

    for _, row in df.iterrows():
        if pd.isna(row["rsi"]) or pd.isna(row["rsi_prev"]) or pd.isna(row["ema"]):
            continue

        # 1) Gestionar posiciones abiertas
        still_open = []
        for pos in open_positions:
            hit_tp = False
            hit_sl = False
            if pos["side"] == "LONG":
                if row["low"] <= pos["sl"]:
                    hit_sl = True
                elif row["high"] >= pos["tp"]:
                    hit_tp = True
            else:
                if row["high"] >= pos["sl"]:
                    hit_sl = True
                elif row["low"] <= pos["tp"]:
                    hit_tp = True

            if hit_tp or hit_sl:
                exit_price = pos["tp"] if hit_tp else pos["sl"]
                if pos["side"] == "LONG":
                    pnl_pct = (exit_price - pos["entry"]) / pos["entry"]
                else:
                    pnl_pct = (pos["entry"] - exit_price) / pos["entry"]
                pnl_usd = SIZE_USDC * pnl_pct
                trades.append({
                    "symbol": symbol,
                    "side": pos["side"],
                    "entry": pos["entry"],
                    "exit": exit_price,
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "result": "tp" if hit_tp else "sl",
                    "opened_at": pos["opened_at"],
                    "closed_at": row["open_time"],
                    "duration_min": int((row["open_time"] - pos["opened_at"]).total_seconds() / 60),
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # 2) Buscar nueva senal
        if len(open_positions) >= MAX_CONCURRENT:
            continue

        cross_down = row["rsi_prev"] >= RSI_LOW and row["rsi"] < RSI_LOW
        cross_up = row["rsi_prev"] <= RSI_HIGH and row["rsi"] > RSI_HIGH

        if cross_down:
            entry = float(row["close"])
            open_positions.append({
                "side": "LONG",
                "entry": entry,
                "tp": entry * (1 + TP_PCT),
                "sl": entry * (1 - SL_PCT),
                "opened_at": row["open_time"],
            })
        elif cross_up:
            entry = float(row["close"])
            open_positions.append({
                "side": "SHORT",
                "entry": entry,
                "tp": entry * (1 - TP_PCT),
                "sl": entry * (1 + SL_PCT),
                "opened_at": row["open_time"],
            })

    return trades


# ---------------- Stats ----------------
def compute_stats(trades):
    if not trades:
        return {"trades": 0}

    df = pd.DataFrame(trades).sort_values("closed_at").reset_index(drop=True)
    n = len(df)
    wins = (df["pnl_usd"] > 0).sum()
    wr = wins / n * 100
    pnl_total = df["pnl_usd"].sum()
    avg_pnl = df["pnl_usd"].mean()
    avg_duration = df["duration_min"].mean()

    df["equity"] = df["pnl_usd"].cumsum()
    df["peak"] = df["equity"].cummax()
    df["drawdown"] = df["equity"] - df["peak"]
    max_dd = df["drawdown"].min()
    max_dd_pct = (max_dd / SIZE_USDC) * 100

    if df["pnl_usd"].std(ddof=1) > 0:
        trades_per_year = n / MONTHS * 12 if MONTHS else n
        sharpe = (df["pnl_usd"].mean() / df["pnl_usd"].std(ddof=1)) * math.sqrt(trades_per_year)
    else:
        sharpe = 0.0

    df["month"] = pd.to_datetime(df["closed_at"]).dt.to_period("M").astype(str)
    by_month = df.groupby("month")["pnl_usd"].sum().sort_values()
    worst_month = (by_month.index[0], float(by_month.iloc[0])) if len(by_month) else (None, 0.0)
    best_month = (by_month.index[-1], float(by_month.iloc[-1])) if len(by_month) else (None, 0.0)

    tp_count = (df["result"] == "tp").sum()
    sl_count = (df["result"] == "sl").sum()

    return {
        "trades": int(n),
        "wins": int(wins),
        "losses": int(n - wins),
        "wr_pct": round(float(wr), 2),
        "pnl_total_usd": round(float(pnl_total), 2),
        "avg_pnl_per_trade": round(float(avg_pnl), 4),
        "avg_duration_min": round(float(avg_duration), 1),
        "tp_hits": int(tp_count),
        "sl_hits": int(sl_count),
        "max_drawdown_usd": round(float(max_dd), 2),
        "max_drawdown_pct_of_size": round(float(max_dd_pct), 2),
        "sharpe_annualized": round(float(sharpe), 3),
        "best_month": best_month,
        "worst_month": worst_month,
        "by_month": by_month.round(2).to_dict(),
    }


def fmt_money(v):
    return ("+$" if v >= 0 else "-$") + "{:,.2f}".format(abs(v))


def print_report(symbol, stats):
    print("")
    print("=" * 60)
    print("  " + str(symbol) + "  (" + INTERVAL + ", " + str(MONTHS) + " months)")
    print("=" * 60)
    if stats.get("trades", 0) == 0:
        print("  No trades.")
        return
    print("  Total trades       : " + str(stats["trades"]))
    print("  Wins / Losses      : " + str(stats["wins"]) + " / " + str(stats["losses"]))
    print("  Win Rate           : " + str(stats["wr_pct"]) + "%")
    print("  TP hits / SL hits  : " + str(stats["tp_hits"]) + " / " + str(stats["sl_hits"]))
    print("  PnL total          : " + fmt_money(stats["pnl_total_usd"]))
    print("  Avg PnL per trade  : " + fmt_money(stats["avg_pnl_per_trade"]))
    print("  Avg duration       : " + str(stats["avg_duration_min"]) + " min")
    print("  Max drawdown       : " + fmt_money(stats["max_drawdown_usd"]) + "  (" + str(stats["max_drawdown_pct_of_size"]) + "% of trade size)")
    print("  Sharpe (annualized): " + str(stats["sharpe_annualized"]))
    bm_label, bm_val = stats["best_month"]
    wm_label, wm_val = stats["worst_month"]
    print("  Best month         : " + str(bm_label) + "  " + fmt_money(bm_val))
    print("  Worst month        : " + str(wm_label) + "  " + fmt_money(wm_val))
    print("  PnL by month:")
    for m, v in stats["by_month"].items():
        print("    " + str(m) + "  " + fmt_money(v))


# ---------------- Main ----------------
def main():
    end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    start_ms = end_ms - MONTHS * 30 * 24 * 60 * 60 * 1000

    all_trades = []

    for sym in SYMBOLS:
        print("")
        print("Descargando " + sym + " (" + INTERVAL + ") " + str(MONTHS) + "m...")
        df = fetch_klines(sym, start_ms, end_ms)
        if df.empty:
            print("  No data.")
            continue
        print("  " + str(len(df)) + " velas (" + str(df["open_time"].iloc[0]) + " -> " + str(df["open_time"].iloc[-1]) + ")")
        trades = backtest(df, sym)
        all_trades.extend(trades)
        stats = compute_stats(trades)
        print_report(sym, stats)

    print("")
    print("#" * 60)
    print("  COMBINED (" + ", ".join(SYMBOLS) + ")")
    print("#" * 60)
    print_report("ALL", compute_stats(all_trades))


if __name__ == "__main__":
    main()
