from fastapi import FastAPI
from main import get_signal

app = FastAPI()

@app.get("/")
def root():
    return {"status": "running"}

@app.get("/signal")
def signal():
    return {"signal": get_signal()}
