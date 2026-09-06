"""Local web UI + control API for the softphone.

Run with:
    python -m softphone_client.app
Then open http://localhost:8765 in a browser on the SAME machine (the mic/
speakers used are this machine's, not the browser's - the page is only a
remote control).
"""

import os
import threading

from fastapi import FastAPI, Body
from fastapi.responses import FileResponse
import uvicorn

from .config import DEFAULT_CONFIG
from .softphone import Softphone

app = FastAPI()
phone = Softphone(DEFAULT_CONFIG)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.on_event("startup")
def _startup():
    threading.Thread(target=phone.start, daemon=True).start()


@app.on_event("shutdown")
def _shutdown():
    # Release our REGISTER binding so it doesn't linger server-side across
    # test runs - stale bindings/dialog state can cause spurious 491s later.
    if phone.rtp is not None:
        phone.rtp.stop()
    if phone.sip is not None:
        phone.sip.unregister()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/api/call")
def api_call(payload: dict = Body(...)):
    number = str(payload.get("number", "8000"))
    threading.Thread(target=phone.call, args=(number,), daemon=True).start()
    return {"ok": True}


@app.post("/api/hangup")
def api_hangup():
    threading.Thread(target=phone.hangup, daemon=True).start()
    return {"ok": True}


@app.get("/api/status")
def api_status():
    return phone.get_status()


@app.get("/api/config")
def api_get_config():
    return phone.get_config()


@app.post("/api/config")
def api_set_config(payload: dict = Body(...)):
    new_config = {
        "server_ip": str(payload.get("server_ip", "")).strip(),
        "server_port": int(payload.get("server_port", 5062)),
        "domain": str(payload.get("domain", "")).strip(),
        "username": str(payload.get("username", "")).strip(),
        "password": str(payload.get("password", "")),
    }
    threading.Thread(target=phone.reconfigure, args=(new_config,), daemon=True).start()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
