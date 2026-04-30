from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import threading
import time

app = FastAPI()

stats = {"allies": 0, "axis": 0}

@app.get("/stats")
def get_stats():
    return JSONResponse(stats)

@app.get("/reset")
def reset():
    stats["allies"] = 0
    stats["axis"] = 0
    return {"status": "reset"}

@app.get("/overlay", response_class=HTMLResponse)
def overlay():
    with open("overlay.html", "r") as f:
        return f.read()

def mock_update():
    # Replace this with real RCON polling
    while True:
        time.sleep(5)
        stats["allies"] += 1
        stats["axis"] += 1

threading.Thread(target=mock_update, daemon=True).start()
