"""本地网页界面后端：http://127.0.0.1:7860"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import webbrowser
from pathlib import Path

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .audio import list_input_devices, list_output_devices
from .engine import Engine

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Hub:
    """把后台线程的事件广播给所有打开的网页。"""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.history: list[dict] = []

    def emit(self, ev: dict):
        if ev.get("type") in ("final", "trans_done"):
            self.history.append(ev)
            self.history = self.history[-400:]
        if self.loop is None:
            return
        msg = json.dumps(ev, ensure_ascii=False)
        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(msg)))

    async def _send(self, msg: str):
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


def create_app(cfg: dict | None = None, engine_factory=Engine) -> FastAPI:
    cfg = cfg or load_config()
    hub = Hub()
    engine = engine_factory(cfg, hub.emit)
    app = FastAPI(title="LiveTranslator")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.state.engine = engine
    app.state.hub = hub

    @app.on_event("startup")
    async def _startup():
        hub.loop = asyncio.get_running_loop()
        threading.Thread(target=engine.load, daemon=True).start()

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/history")
    async def history_page():
        return FileResponse(STATIC / "history.html")

    # ---------------- 复盘 / 记账本 ----------------
    @app.get("/api/log/days")
    async def log_days():
        return await asyncio.to_thread(engine.logbook.days)

    @app.get("/api/log/day/{date}")
    async def log_day(date: str):
        return await asyncio.to_thread(engine.logbook.day, date)

    @app.get("/api/log/search")
    async def log_search(q: str = ""):
        return await asyncio.to_thread(engine.logbook.search, q)

    @app.post("/api/log/edit")
    async def log_edit(body: dict):
        try:
            engine.logbook.edit(body["rid"], body["field"], body.get("value"))
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.get("/api/log/audio")
    async def log_audio(path: str):
        p = engine.logbook.audio_path(path)
        if p is None:
            return Response(status_code=404)
        return FileResponse(p, media_type="audio/wav")

    @app.get("/api/state")
    async def state():
        return {
            "ready": engine.ready,
            "status": engine.status,
            "running": engine.running,
            "opts": engine.opts,
            "my_lang": engine.my_lang,
            "other_lang": engine.other_lang,
            "tts": engine.tts is not None,
            "inputs": list_input_devices(),
            "outputs": list_output_devices(),
            "output_device": str(engine.cfg.get("tts", {}).get("output_device", "default")),
            "history": hub.history[-200:],
        }

    @app.post("/api/start")
    async def start(body: dict):
        try:
            await asyncio.to_thread(engine.start, body)
            return {"ok": True}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    @app.post("/api/stop")
    async def stop():
        await asyncio.to_thread(engine.stop)
        return {"ok": True}

    @app.post("/api/translate")
    async def translate(body: dict):
        tid = engine.translate_text(body.get("text", ""), bool(body.get("speak", True)))
        return {"ok": True, "id": tid}

    @app.post("/api/speak")
    async def speak(body: dict):
        engine.speak(body.get("text", ""), body.get("lang", engine.other_lang), body.get("id", ""))
        return {"ok": True}

    @app.post("/api/lang")
    async def set_lang(body: dict):
        lang = body.get("other_lang")
        if lang in ("fr", "en"):
            engine.other_lang = lang
        return {"ok": True, "other_lang": engine.other_lang}

    @app.post("/api/output")
    async def set_output(body: dict):
        engine.cfg.setdefault("tts", {})["output_device"] = body.get("device", "default")
        return {"ok": True}

    @app.post("/api/reset")
    async def reset():
        if engine.translator:
            engine.translator.reset_context()
        hub.history.clear()
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        hub.clients.add(websocket)
        try:
            await websocket.send_text(json.dumps(
                {"type": "status", "text": engine.status, "ready": engine.ready}, ensure_ascii=False))
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(websocket)

    return app


def main():
    import uvicorn

    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 运行时完全离线
    cfg = load_config()
    scfg = cfg.get("server", {})
    host, port = scfg.get("host", "127.0.0.1"), int(scfg.get("port", 7860))
    app = create_app(cfg)
    url = f"http://127.0.0.1:{port}"
    print(f"\n  LiveTranslator is running → {url}\n  (close this window to quit)\n", flush=True)
    if scfg.get("open_browser", True):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
