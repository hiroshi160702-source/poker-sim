from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import HoldemGame
from .selfplay import run_heads_up_cpu_match, run_multiway_cpu_match

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
LOGS_DIR = BASE_DIR.parent / "logs"
EMBEDDED_CPU_DIR = BASE_DIR.parent / "embedded_cpus"

app = FastAPI(title="Texas Hold'em Simulator", version="0.1.0")
game = HoldemGame(LOGS_DIR, EMBEDDED_CPU_DIR)


class ActionRequest(BaseModel):
    action: str
    amount: Optional[int] = None


class CpuLoadRequest(BaseModel):
    seat: int
    path: str


class TableConfigRequest(BaseModel):
    starting_stack: int
    cpu_count: int


class EmbeddedCpuRequest(BaseModel):
    seat: int
    code: str


class CpuMatchRequest(BaseModel):
    hero_cpu_path: str
    villain_cpu_path: str
    hands: int = 100
    starting_stack: int = 2000
    export_strategy_path: Optional[str] = None


class CpuMultiMatchRequest(BaseModel):
    cpu_paths: list[str]
    hands: int = 100
    starting_stack: int = 2000
    export_strategy_path: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state(reveal_folded: bool = Query(False)) -> dict:
    return game.serialize_state(reveal_folded=reveal_folded)


@app.post("/api/new-hand")
async def new_hand() -> dict:
    game.start_new_hand()
    return game.serialize_state()


@app.post("/api/action")
async def action(request: ActionRequest) -> dict:
    if game.current_turn is None:
        raise HTTPException(status_code=400, detail="No active turn.")

    current = game.players[game.current_turn]
    if not current.is_human:
        raise HTTPException(status_code=400, detail="Waiting for CPU turn.")

    try:
        game.apply_player_action(current.seat, request.action, request.amount)
        game.auto_play_until_human()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return game.serialize_state()


@app.post("/api/load-cpu")
async def load_cpu(request: CpuLoadRequest) -> dict:
    try:
        game.load_cpu(request.seat, request.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return game.serialize_state()


@app.post("/api/reset-table")
async def reset_table() -> dict:
    game.reset_table()
    return game.serialize_state()


@app.post("/api/configure-table")
async def configure_table(request: TableConfigRequest) -> dict:
    try:
        game.configure_table(request.starting_stack, request.cpu_count)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return game.serialize_state()


@app.post("/api/save-cpu-code")
async def save_cpu_code(request: EmbeddedCpuRequest) -> dict:
    try:
        game.save_embedded_cpu(request.seat, request.code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return game.serialize_state()


@app.post("/api/run-cpu-match")
async def run_cpu_match(request: CpuMatchRequest) -> dict:
    try:
        return run_heads_up_cpu_match(
            logs_dir=LOGS_DIR,
            embedded_cpu_dir=EMBEDDED_CPU_DIR,
            hero_cpu_path=request.hero_cpu_path,
            villain_cpu_path=request.villain_cpu_path,
            hands=request.hands,
            starting_stack=request.starting_stack,
            export_strategy_path=request.export_strategy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/run-cpu-multiplayer")
async def run_cpu_multiplayer(request: CpuMultiMatchRequest) -> dict:
    try:
        return run_multiway_cpu_match(
            logs_dir=LOGS_DIR,
            embedded_cpu_dir=EMBEDDED_CPU_DIR,
            cpu_paths=request.cpu_paths,
            hands=request.hands,
            starting_stack=request.starting_stack,
            export_strategy_path=request.export_strategy_path,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
