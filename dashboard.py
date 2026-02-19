import streamlit as st
import pandas as pd
import requests
import os
import time
from datetime import datetime, timedelta
import pytz
import numpy as np

st.set_page_config(page_title="ORB Options Scanner ELITE", layout="wide")

REFRESH_SECONDS = 300
TICKERS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "META"]

ACCOUNT_SIZE = 30000
RISK_PER_TRADE_PERCENT = 10  # 1% risk

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

if "alerted" not in st.session_state:
    st.session_state.alerted = {}

def market_is_open():
    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 16:
        return False
    return True

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message})

def get_intraday(symbol):
    end = datetime.utcnow()
    start = end - timedelta(hours=6)

    url = f"{BASE_URL}/stocks/{symbol}/bars"
    params = {
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "timeframe": "1Min",
        "feed": "iex"
    }

    r = requests.get(url, headers=HEADERS, params=params)

    if r.status_code == 200:
        bars = r.json().get("bars", [])
        if not bars:
            return None
        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df.rename(columns={"t": "Time", "c": "Close", "v": "Volume", "h": "High", "l": "Low"}, inplace=True)
        return df
    return None

def calculate_atr(df, period=14):
    df["TR"] = np.maximum(df["High"] - df["Low"],
                 np.maximum(abs(df["High"] - df["Close"].shift()),
                            abs(df["Low"] - df["Close"].shift())))
    df["ATR"] = df["TR"].rolling(period).mean()
    return df

def calculate_vwap(df):
    df["CumVol"] = df["Volume"].cumsum()
    df["CumVolPrice"] = (df["Close"] * df["Volume"]).cumsum()
    df["VWAP"] = df["CumVolPrice"] / df["CumVol"]
    return df

def calculate_rsi(df, period=14):
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df

def analyze(df):
    df = calculate_vwap(df)
    df = calculate_rsi(df)
    df = calculate_atr(df)

    opening_range = df.iloc[:15]
    orb_high = opening_range["High"].max()
    orb_low = opening_range["Low"].min()

    last_price = df["Close"].iloc[-1]
    atr = df["ATR"].iloc[-1]
    rsi = df["RSI"].iloc[-1]
    vwap = df["VWAP"].iloc[-1]

    probability = 50
    bias = None

    if last_price > orb_high:
        probability += 20
        bias = "LONG"
    elif last_price < orb_low:
        probability += 20
        bias = "SHORT"

    if last_price > vwap:
        probability += 10
    if rsi > 60:
        probability += 10

    probability = min(probability, 95)

    return bias, probability, orb_high, orb_low, atr

def generate_trade_plan(price, atr, bias):
    risk_amount = ACCOUNT_SIZE * (RISK_PER_TRADE_PERCENT / 100)

    if bias == "LONG":
        stop = price - atr
        target = price + (2 * atr)
    else:
        stop = price + atr
        target = price - (2 * atr)

    risk_per_share = abs(price - stop)
    position_size = risk_amount / risk_per_share

    return round(stop,2), round(target,2), int(position_size)

def generate_option_ideas(price, bias):
    strike = round(price)
    if bias == "LONG":
        return f"{strike}C", f"{strike+5}C (30-45 DTE)"
    else:
        return f"{strike}P", f"{strike-5}P (30-45 DTE)"

st.title("🚨 ORB Options Scanner ELITE")

if not market_is_open():
    st.warning("Market Closed — Scanner Paused")
else:
    for ticker in TICKERS:
        df = get_intraday(ticker)
        if df is None or len(df) < 30:
            continue

        bias, probability, orb_high, orb_low, atr = analyze(df)

        if bias is None or probability < 70:
            continue

        last_price = df["Close"].iloc[-1]
        stop, target, size = generate_trade_plan(last_price, atr, bias)
        zero_dte, thirty_dte = generate_option_ideas(last_price, bias)

        st.markdown("---")
        st.subheader(ticker)
        st.write(f"Price: ${round(last_price,2)}")
        st.write(f"Bias: {bias}")
        st.write(f"Probability: {probability}%")
        st.write(f"Stop: {stop}")
        st.write(f"Target: {target}")
        st.write(f"Position Size (shares equiv): {size}")
        st.write(f"0DTE: {zero_dte}")
        st.write(f"30DTE: {thirty_dte}")

        if probability >= 80 and ticker not in st.session_state.alerted:
            message = f"""
🚨 ORB ELITE SIGNAL 🚨

Ticker: {ticker}
Bias: {bias}
Entry: {round(last_price,2)}
Stop: {stop}
Target: {target}
Probability: {probability}%

0DTE: {zero_dte}
30DTE: {thirty_dte}
"""
            send_telegram_alert(message)
            st.session_state.alerted[ticker] = True
