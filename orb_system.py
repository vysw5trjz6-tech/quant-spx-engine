import yfinance as yf
import pandas as pd
import numpy as np

# ==============================
# CONFIGURATION
# ==============================

TICKER = "SPY"
PERIOD = "6mo"
INTERVAL = "5m"

RISK_MULTIPLIER = 2        # 2R profit target
RISK_PERCENT = 0.01        # 1% account risk per trade
ACCOUNT_SIZE = 25000       # Change this to your account size
VOL_FILTER_THRESHOLD = 0.01  # 1% daily range filter


# ==============================
# RISK ENGINE
# ==============================

def position_size(account_size, risk_percent, stop_distance):
    risk_amount = account_size * risk_percent
    size = risk_amount / stop_distance
    return round(size, 2)


# ==============================
# VOLATILITY FILTER
# ==============================

def volatility_filter(day):
    day_range = day["High"].max() - day["Low"].min()
    atr_estimate = day_range / day["Close"].mean()
    return atr_estimate > VOL_FILTER_THRESHOLD


# ==============================
# BACKTEST ENGINE
# ==============================

def backtest_orb():

    print("\nDownloading data...")
    data = yf.download(TICKER, period=PERIOD, interval=INTERVAL)
    data = data.dropna()

    if data.empty:
        print("No data found.")
        return

    data["Date"] = data.index.date
    results = []
    trade_details = []

    grouped = data.groupby("Date")

    for date, day in grouped:

        if len(day) < 20:
            continue

        if not volatility_filter(day):
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

            # Long breakout
            if position is None and candle["High"] > orb_high:
                position = "LONG"
                entry = orb_high
                stop = orb_low
                target = entry + (range_size * RISK_MULTIPLIER)

            # Short breakout
            elif position is None and candle["Low"] < orb_low:
                position = "SHORT"
                entry = orb_low
                stop = orb_high
                target = entry - (range_size * RISK_MULTIPLIER)

            if position == "LONG":
                if candle["Low"] <= stop:
                    results.append(-1)
                    trade_details.append(("LOSS", date))
                    break
                if candle["High"] >= target:
                    results.append(RISK_MULTIPLIER)
                    trade_details.append(("WIN", date))
                    break

            if position == "SHORT":
                if candle["High"] >= stop:
                    results.append(-1)
                    trade_details.append(("LOSS", date))
                    break
                if candle["Low"] <= target:
                    results.append(RISK_MULTIPLIER)
                    trade_details.append(("WIN", date))
                    break

    if len(results) == 0:
        print("No trades triggered.")
        return

    results = np.array(results)

    win_rate = (results > 0).mean()
    avg_r = results.mean()
    total_r = results.sum()

    equity_curve = np.cumsum(results)
    max_drawdown = np.min(equity_curve - np.maximum.accumulate(equity_curve))

    # Risk model example
    avg_stop_distance = 1.0  # Approx estimate for position sizing example
    example_size = position_size(ACCOUNT_SIZE, RISK_PERCENT, avg_stop_distance)

    print("\n==============================")
    print("ORB BACKTEST RESULTS")
    print("==============================")
    print("Trades:", len(results))
    print("Win Rate:", round(win_rate * 100, 2), "%")
    print("Average R per trade:", round(avg_r, 2))
    print("Total R:", round(total_r, 2))
    print("Max Drawdown (R):", round(max_drawdown, 2))
    print("------------------------------")
    print("Example Position Size @ 1% risk:", example_size, "shares")
    print("==============================\n")


# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    backtest_orb()
