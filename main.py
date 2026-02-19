import yfinance as yf

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
