import streamlit as st
import sqlite3
import pandas as pd

st.title("📊 Strategy Analytics")

conn = sqlite3.connect("trades.db")
df = pd.read_sql("SELECT * FROM trades", conn)
conn.close()

if df.empty:
    st.write("No trades logged yet.")
else:
    st.dataframe(df)
    st.metric("Total Trades", len(df))
    st.bar_chart(df["mode"].value_counts())
