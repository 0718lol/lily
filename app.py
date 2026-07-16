import asyncio
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import TaskExecutor
from config import settings
from database import Database


db = Database(settings.db_path)
executor = TaskExecutor(settings)
connections: set[WebSocket] = set()


class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=10, max_length=8000)
    repository: str = Field(default="", max_length=500)
    repository_path: str = Field(default="", max_length=1200)
    issue_url: str = Field(default="", max_length=1000)
    priority: int = Field(default=2, ge=1, le=3)
    risk: str = Field(default="low", pattern="^(low|medium|high)$")


class PauseRequest(BaseModel):
    paused: bool


async def broadcast(event: str = "refresh") -> None:
    dead: list[WebSocket] = []
    for connection in connections:
        try:
            await connection.send_json({"event": event})
        except Exception:
            dead.append(connection)
    for connection in dead:
        connections.discard(connection)


async def process_task(task: dict[str, Any]) -> None:
    total_input = task["input_tokens"]
    total_output = task["output_tokens"]
    try:
        mode = executor.resolve_mode(task)
        db.update_task(task["id"], executor_mode=mode)
        if mode == "codex-cli":
            db.add_event("codex.started", "Codex 已在隔离工作树中开始执行", task["id"])
            await broadcast("codex.started")
        async for result in executor.run(task):
            total_input += result.input_tokens
            total_output += result.output_tokens
            updates = {
                result.key: result.content,
                "input_tokens": total_input,
                "output_tokens": total_output,
            }
            updates.update(result.metadata)
            db.update_task(
                task["id"],
                **updates,
            )
            db.add_event(
                "stage.completed",
                f"{result.label}已完成",
                task["id"],
                {"stage": result.key},
            )
            await broadcast("task.updated")
        db.complete_task(task["id"])
    except Exception as exc:
        db.fail_task(task["id"], str(exc))
    await broadcast("task.updated")


async def worker_loop() -> None:
    while True:
        if not db.is_paused():
            task = db.claim_next_task()
            if task:
                await broadcast("task.started")
                await process_task(task)
                continue
        await asyncio.sleep(settings.worker_interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    task = asyncio.create_task(worker_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Lily OpenMaintainer", version="0.2.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=settings.root / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(settings.root / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "mode": executor.mode,
        "model": executor.model_label,
        "codex_available": bool(executor.codex and executor.codex.available),
        "paused": db.is_paused(),
    }


@app.get("/api/dashboard")
async def dashboard():
    payload = db.dashboard()
    payload.update({
        "mode": executor.mode,
        "model": executor.model_label,
        "codex_available": bool(executor.codex and executor.codex.available),
        "allowed_repo_root": str(settings.allowed_repo_root),
    })
    return payload


@app.get("/api/tasks")
async def list_tasks():
    return db.list_tasks()


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.post("/api/tasks", status_code=201)
async def create_task(payload: TaskCreate):
    task = db.create_task(payload.model_dump(), settings.max_attempts)
    await broadcast("task.created")
    return task


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str):
    try:
        task = db.set_decision(task_id, True)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await broadcast("task.approved")
    return task


@app.post("/api/tasks/{task_id}/reject")
async def reject_task(task_id: str):
    try:
        task = db.set_decision(task_id, False)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await broadcast("task.rejected")
    return task


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    try:
        task = db.retry_task(task_id)
    except KeyError:
        raise HTTPException(404, "任务不存在") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    await broadcast("task.retried")
    return task


@app.get("/api/events")
async def events():
    return db.get_events()


@app.post("/api/control/pause")
async def pause(payload: PauseRequest):
    state = db.set_paused(payload.paused)
    await broadcast("system.control")
    return {"paused": state}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.discard(websocket)
