import yfinance as yf

def get_signal():

    data = yf.download("SPY", period="1d", interval="5m")

    orb_high = data["High"].iloc[:3].max()
    orb_low = data["Low"].iloc[:3].min()

    price = data["Close"].iloc[-1]

    if price > orb_high:
        return "CALL"

    if price < orb_low:
        return "PUT"

    return "NO TRADE"
