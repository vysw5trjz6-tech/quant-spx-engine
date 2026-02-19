import streamlit as st
import pandas as pd
import requests
import os
import time
from datetime import datetime, timedelta

st.set_page_config(page_title="ORB Options Scanner", layout="wide")

# =============================
# CONFIG
# =============================

REFRESH_SECONDS = 300  # 5 minute auto scan
TICKERS = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "META"]

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

# =============================
# AUTO REFRESH
# =============================

if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = time.time()

if time.time() - st.session_state.last_refresh > REFRESH_SECONDS:
    st.session_state.last_refresh = time.time()
    st.rerun()

# =============================
# TELEGRAM ALERT
# =============================

def send_telegram_alert(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }
    requests.post(url, data=payload)

# =============================
# GET INTRADAY DATA
# =============================

def get_intraday(symbol):
    end = datetime.utcnow()
    start = end - timedelta(hours=2)

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
        df.rename(columns={"t": "Time", "c": "Close", "v": "Volume"}, inplace=True)
        return df
    return None

# =============================
# PROBABILITY MODEL
# =============================

def calculate_probability(df):
    if len(df) < 30:
        return None

    opening_high = df["Close"].iloc[:15].max()
    session_high = df["Close"].max()
    session_low = df["Close"].min()
    last_price = df["Close"].iloc[-1]

    range_expansion = session_high - opening_high
    trend_strength = (last_price - session_low) / (session_high - session_low)

    probability = 50

    if range_expansion > 0:
        probability += 15

    if trend_strength > 0.7:
        probability += 20

    if df["Volume"].iloc[-1] > df["Volume"].mean():
        probability += 10

    return min(round(probability), 95)

# =============================
# OPTION SUGGESTION
# =============================

def generate_option_ideas(symbol, price, bias):
    strike = round(price)

    if bias == "LONG":
        zero_dte = f"{strike}C"
        thirty_dte = f"{strike+5}C (30-45 DTE)"
    else:
        zero_dte = f"{strike}P"
        thirty_dte = f"{strike-5}P (30-45 DTE)"

    return zero_dte, thirty_dte

# =============================
# SCANNER
# =============================

st.title("🚨 ORB Multi-Ticker Options Scanner")

for ticker in TICKERS:
    df = get_intraday(ticker)

    if df is None:
        continue

    probability = calculate_probability(df)

    if probability is None:
        continue

    last_price = df["Close"].iloc[-1]
    bias = "LONG" if df["Close"].iloc[-1] > df["Close"].iloc[0] else "SHORT"

    zero_dte, thirty_dte = generate_option_ideas(ticker, last_price, bias)

    st.markdown("---")
    st.subheader(f"{ticker}")
    st.write(f"Price: ${round(last_price,2)}")
    st.write(f"Bias: {bias}")
    st.write(f"Probability: {probability}%")
    st.write(f"0DTE Idea: {zero_dte}")
    st.write(f"30DTE Idea: {thirty_dte}")

    if probability >= 75:
        message = f"""
🚨 TRADE IDEA 🚨

Ticker: {ticker}
Bias: {bias}
Entry: {round(last_price,2)}
Probability: {probability}%

0DTE: {zero_dte}
30DTE: {thirty_dte}
"""
        send_telegram_alert(message)
