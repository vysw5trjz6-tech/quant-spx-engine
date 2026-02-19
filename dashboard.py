import streamlit as st
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
import pytz
import numpy as np
from database import init_db, log_trade

st.set_page_config(page_title="Quant Engine Scanner", layout="wide")

init_db()

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

def send_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message}
        )

def get_data(symbol):
    end = datetime.utcnow()
    start = end - timedelta(hours=6)

    r = requests.get(
        f"{BASE_URL}/stocks/{symbol}/bars",
        headers=HEADERS,
        params={
            "start": start.isoformat()+"Z",
            "end": end.isoformat()+"Z",
            "timeframe": "1Min",
            "feed": "iex"
        }
    )

    if r.status_code != 200:
        return None

    bars = r.json().get("bars", [])
    if not bars:
        return None

    df = pd.DataFrame(bars)
    df.rename(columns={"c":"Close","h":"High","l":"Low","v":"Volume"}, inplace=True)
    return df

def add_indicators(df):
    df["VWAP"] = (df["Close"]*df["Volume"]).cumsum() / df["Volume"].cumsum()
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(14).mean()/loss.rolling(14).mean()
    df["RSI"] = 100 - (100/(1+rs))

    tr = np.maximum(df["High"]-df["Low"],
        np.maximum(abs(df["High"]-df["Close"].shift()),
                   abs(df["Low"]-df["Close"].shift())))
    df["ATR"] = tr.rolling(14).mean()
    return df

def get_vix_regime():
    df = get_data("VIXY")
    if df is None:
        return "UNKNOWN"
    vix = df.iloc[-1]["Close"]
    if vix < 18:
        return "LOW"
    elif vix < 25:
        return "NORMAL"
    return "HIGH"

st.title("🚀 Quant Trading Engine")

if not market_is_open():
    st.warning("Market Closed — Scanner Paused")
else:
    vol_regime = get_vix_regime()
    st.info(f"Volatility Regime: {vol_regime}")

    for ticker in TICKERS:
        if ticker == "SPY":
            continue

        df = get_data(ticker)
        if df is None or len(df)<30:
            continue

        df = add_indicators(df)

        opening = df.iloc[:15]
        orb_high = opening["High"].max()
        orb_low = opening["Low"].min()

        last = df.iloc[-1]
        price = last["Close"]
        rsi = last["RSI"]
        atr = last["ATR"]

        bias=None
        mode=None

        if price>orb_high:
            bias="LONG"
            mode="ORB"
        elif price<orb_low:
            bias="SHORT"
            mode="ORB"

        if vol_regime=="HIGH" and rsi>65:
            mode="SCALP"

        if bias is None:
            continue

        stop = price-atr if bias=="LONG" else price+atr
        target = price+(2*atr) if mode=="ORB" else price+atr

        probability=80 if mode=="SCALP" else 75

        st.markdown("---")
        st.subheader(f"{ticker} — {mode}")
        st.write(f"Entry: {round(price,2)}")
        st.write(f"Stop: {round(stop,2)}")
        st.write(f"Target: {round(target,2)}")
        st.write(f"Probability: {probability}%")

        key=f"{ticker}_{mode}"
        if key not in st.session_state.alerted:
            message=f"""
🚨 {mode} SIGNAL 🚨
Ticker: {ticker}
Entry: {round(price,2)}
Stop: {round(stop,2)}
Target: {round(target,2)}
Vol Regime: {vol_regime}
"""
            send_telegram(message)
            log_trade(ticker,mode,bias,price,stop,target,probability,vol_regime)
            st.session_state.alerted[key]=True
