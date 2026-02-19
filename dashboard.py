import streamlit as st
import pandas as pd
import requests
import os
from datetime import datetime, timedelta
import time

st.set_page_config(page_title="ORB Strategy Dashboard", layout="wide")

# =============================
# CONFIG
# =============================
REFRESH_SECONDS = 5

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

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
# GET LIVE PRICE (FREE IEX FEED)
# =============================
def get_live_price(symbol):
    url = f"{BASE_URL}/stocks/{symbol}/quotes/latest"
    params = {"feed": "iex"}  # REQUIRED for free accounts

    response = requests.get(url, headers=HEADERS, params=params)

    if response.status_code == 200:
        data = response.json()
        return data["quote"]["ap"]
    else:
        st.write("Error:", response.status_code)
        st.write(response.text)
        return None

# =============================
# GET INTRADAY DATA
# =============================
def get_intraday_data(symbol):
    end = datetime.utcnow()
    start = end - timedelta(hours=2)

    url = f"{BASE_URL}/stocks/{symbol}/bars"

    params = {
        "start": start.isoformat() + "Z",
        "end": end.isoformat() + "Z",
        "timeframe": "1Min",
        "feed": "iex"  # REQUIRED for free accounts
    }

    response = requests.get(url, headers=HEADERS, params=params)

    if response.status_code == 200:
        bars = response.json().get("bars", [])
        if len(bars) == 0:
            return None

        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df.rename(columns={"t": "Time", "c": "Close"}, inplace=True)
        return df[["Time", "Close"]]
    else:
        st.write("Error:", response.status_code)
        st.write(response.text)
        return None

# =============================
# ORB STRATEGY
# =============================
def run_orb(df):
    opening_range = df["Close"].iloc[:15].max()
    breakout = df["Close"].max()
    return round(opening_range, 2), round(breakout, 2), round(breakout - opening_range, 2)

# =============================
# UI
# =============================
st.title("📊 ORB Strategy Dashboard (Live via Alpaca)")

symbol = st.text_input("Enter Stock Symbol", "AAPL").upper()

col1, col2 = st.columns(2)

with col1:
    st.subheader("Live Price")

    if not ALPACA_KEY or not ALPACA_SECRET:
        st.error("API keys not detected in Railway environment variables.")
    else:
        price = get_live_price(symbol)

        if price:
            st.metric(symbol, f"${round(price,2)}")
            st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")
        else:
            st.error("Could not fetch live price.")

with col2:
    st.subheader("Run ORB Backtest")

    if st.button("Run ORB Strategy"):
        with st.spinner("Fetching data & running ORB..."):
            df = get_intraday_data(symbol)

            if df is not None and len(df) > 20:
                opening, breakout, profit = run_orb(df)

                st.success("ORB Calculated")
                st.write(f"Opening Range High: ${opening}")
                st.write(f"Session High: ${breakout}")
                st.write(f"Breakout Distance: ${profit}")

                st.line_chart(df.set_index("Time"))
            else:
                st.error("Not enough data available or API issue.")

st.markdown("---")
st.caption("Using Alpaca free IEX data feed.")
