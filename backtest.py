import yfinance as yf
import pandas as pd
import numpy as np

RISK_MULTIPLIER = 2  # 2R target

def backtest_orb():
    data = yf.download("SPY", period="6mo", interval="5m")
    data = data.dropna()

    data["Date"] = data.index.date
    results = []

    grouped = data.groupby("Date")

    for date, day in grouped:

        if len(day) < 20:
            continue

        orb = day.iloc[:3]
        orb_high = orb["High"].max()
        orb_low = orb["Low"].min()
        range_size = orb_high - orb_low

        position = None
        entry = 0
        stop = 0
        target = 0

        for i in range(3, len(day)):
            candle = day.iloc[i]

            # Breakout long
            if position is None and candle["High"] > orb_high:
                position = "LONG"
                entry = orb_high
                stop = orb_low
                target = entry + (range_size * RISK_MULTIPLIER)

            # Breakout short
            elif position is None and candle["Low"] < orb_low:
                position = "SHORT"
                entry = orb_low
                stop = orb_high
                target = entry - (range_size * RISK_MULTIPLIER)

            if position == "LONG":
                if candle["Low"] <= stop:
                    results.append(-1)
                    break
                if candle["High"] >= target:
                    results.append(RISK_MULTIPLIER)
                    break

            if position == "SHORT":
                if candle["High"] >= stop:
                    results.append(-1)
                    break
                if candle["Low"] <= target:
                    results.append(RISK_MULTIPLIER)
                    break

    results = np.array(results)

    win_rate = (results > 0).mean()
    avg_r = results.mean()
    total_r = results.sum()

    print("Trades:", len(results))
    print("Win rate:", round(win_rate * 100, 2), "%")
    print("Avg R:", round(avg_r, 2))
    print("Total R:", round(total_r, 2))

if __name__ == "__main__":
    backtest_orb()
def volatility_filter(day):
    day_range = day["High"].max() - day["Low"].min()
    atr_estimate = day_range / day["Close"].mean()
    return atr_estimate > 0.01  # Only trade >1% range days
if not volatility_filter(day):
    continue
