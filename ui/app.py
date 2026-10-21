from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client import DFSClient
import protocol as p

from .process_manager import ProcessManager
from .state import event_bus


app = FastAPI(title="pyDFS Demo UI")
templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
manager = ProcessManager(PROJECT_ROOT)


class WriteRequest(BaseModel):
    path: str
    content: str


class PathRequest(BaseModel):
    path: str


@app.on_event("startup")
async def startup() -> None:
    event_bus.bind_loop(asyncio.get_running_loop())
    event_bus.emit("ui", "Demo UI started")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"page": "dashboard"},
    )


@app.get("/client", response_class=HTMLResponse)
async def client_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="client.html",
        context={"page": "client"},
    )


@app.get("/node/{node_name}", response_class=HTMLResponse)
async def node_page(request: Request, node_name: str):
    if node_name not in manager.nodes:
        raise HTTPException(status_code=404, detail="Unknown node")
    return templates.TemplateResponse(
        request=request,
        name="node.html",
        context={"page": "node", "node_name": node_name},
    )


@app.get("/api/status")
async def status():
    return manager.status()


@app.post("/api/start/{node_name}")
async def start_node(node_name: str):
    try:
        return manager.start(node_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/stop/{node_name}")
async def stop_node(node_name: str):
    try:
        return manager.stop(node_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/restart/{node_name}")
async def restart_node(node_name: str):
    try:
        return manager.restart(node_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/reset")
async def reset_demo():
    return manager.reset_storage()


@app.post("/api/start-cluster")
async def start_cluster():
    primary = manager.start("primary")
    manager.wait_for_role("primary", {"PRIMARY"}, timeout=6.0)
    backup = manager.start("backup")
    return {"primary": primary, "backup": backup}


@app.post("/api/stop-cluster")
async def stop_cluster():
    backup = manager.stop("backup")
    primary = manager.stop("primary")
    return {"primary": primary, "backup": backup}


@app.post("/api/client/read")
async def read_file(payload: PathRequest):
    _validate_path(payload.path)
    response = DFSClient().read(payload.path)
    event_bus.emit("client", f"READ {payload.path} -> {response}")
    return _response_payload(response)


@app.post("/api/client/write")
async def write_file(payload: WriteRequest):
    _validate_path(payload.path)
    response = DFSClient().write(payload.path, payload.content)
    event_bus.emit("client", f"WRITE {payload.path} -> {response}")
    return _response_payload(response)


@app.post("/api/client/update")
async def update_file(payload: WriteRequest):
    _validate_path(payload.path)
    response = DFSClient().update(payload.path, payload.content)
    event_bus.emit("client", f"UPDATE {payload.path} -> {response}")
    return _response_payload(response)


@app.post("/api/client/delete")
async def delete_file(payload: PathRequest):
    _validate_path(payload.path)
    response = DFSClient().delete(payload.path)
    event_bus.emit("client", f"DELETE {payload.path} -> {response}")
    return _response_payload(response)


@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    await websocket.accept()
    for event in event_bus.recent():
        await websocket.send_json(event)
    try:
        async for queue in event_bus.subscribe():
            while True:
                await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        return


def _response_payload(response: str) -> dict[str, str | bool]:
    cmd, *args = p.parse(response)
    ok = cmd == "OK"
    return {
        "ok": ok,
        "raw": response,
        "payload": args[0] if args else "",
    }


def _validate_path(path: str) -> None:
    if not path.strip():
        raise HTTPException(status_code=400, detail="Path is required")
