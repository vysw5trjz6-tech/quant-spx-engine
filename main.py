from flask import Flask
import yfinance as yf

app = Flask(__name__)

def get_signal():
    try:
        data = yf.download("SPY", period="1d", interval="5m")

        if data.empty:
            return "NO DATA"

        if len(data) < 3:
            return "WAITING FOR DATA"

        orb_high = data["High"].iloc[:3].max()
        orb_low = data["Low"].iloc[:3].min()
        price = data["Close"].iloc[-1]

        if price > orb_high:
            return "CALL"

        if price < orb_low:
            return "PUT"

        return "NO TRADE"

    except Exception as e:
        return f"ERROR: {str(e)}"

@app.route("/")
def home():
    signal = get_signal()
    return f"<h1>SPY Signal: {signal}</h1>"

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
