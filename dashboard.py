import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

st.set_page_config(page_title="ORB Trading Dashboard", layout="wide")

st.title("📊 ORB Strategy Dashboard")

# Sidebar controls
st.sidebar.header("Strategy Settings")

ticker = st.sidebar.text_input("Ticker", "SPY")
period = st.sidebar.selectbox("Backtest Period", ["3mo", "6mo", "1y"])
risk_multiple = st.sidebar.slider("Reward (R Multiple)", 1.0, 4.0, 2.0, 0.5)
vol_filter_threshold = st.sidebar.slider("Volatility Filter %", 0.005, 0.03, 0.01, 0.001)
account_size = st.sidebar.number_input("Account Size", value=25000)

def volatility_filter(day):
    day_range = day["High"].max() - day["Low"].min()
    atr_estimate = day_range / day["Close"].mean()
    return atr_estimate > vol_filter_threshold

def backtest():

    data = yf.download(ticker, period=period, interval="5m")
    data = data.dropna()

    if data.empty:
        st.error("No data found.")
        return None, None

    data["Date"] = data.index.date
    results = []

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

        for i in range(3, len(day)):
            candle = day.iloc[i]

            if position is None and candle["High"] > orb_high:
                position = "LONG"
                entry = orb_high
                stop = orb_low
                target = entry + (range_size * risk_multiple)

            elif position is None and candle["Low"] < orb_low:
                position = "SHORT"
                entry = orb_low
                stop = orb_high
                target = entry - (range_size * risk_multiple)

            if position == "LONG":
                if candle["Low"] <= stop:
                    results.append(-1)
                    break
                if candle["High"] >= target:
                    results.append(risk_multiple)
                    break

            if position == "SHORT":
                if candle["High"] >= stop:
                    results.append(-1)
                    break
                if candle["Low"] <= target:
                    results.append(risk_multiple)
                    break

    if len(results) == 0:
        return None, None

    results = np.array(results)
    equity_curve = np.cumsum(results)

    stats = {
        "Trades": len(results),
        "Win Rate (%)": round((results > 0).mean() * 100, 2),
        "Average R": round(results.mean(), 2),
        "Total R": round(results.sum(), 2),
        "Max Drawdown (R)": round(np.min(equity_curve - np.maximum.accumulate(equity_curve)), 2)
    }

    return stats, equity_curve

if st.button("Run Backtest"):

    stats, equity_curve = backtest()

    if stats is None:
        st.warning("No trades triggered.")
    else:
        col1, col2, col3, col4, col5 = st.columns(5)

        col1.metric("Trades", stats["Trades"])
        col2.metric("Win Rate", f'{stats["Win Rate (%)"]}%')
        col3.metric("Avg R", stats["Average R"])
        col4.metric("Total R", stats["Total R"])
        col5.metric("Max DD", stats["Max Drawdown (R)"])

        st.subheader("Equity Curve")

        fig, ax = plt.subplots()
        ax.plot(equity_curve)
        ax.set_title("Equity Curve (R)")
        ax.set_xlabel("Trade Number")
        ax.set_ylabel("Cumulative R")
        st.pyplot(fig)
