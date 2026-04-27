from __future__ import annotations

"""ローカル版と公開版の両方で使う FastAPI の入口です。"""

import re
import sys
import threading
import uuid
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import HoldemGame
from .selfplay import run_multiway_cpu_match

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from tools.strategy import build_and_save_strategy_table

STATIC_DIR = BASE_DIR / "static"
LOGS_DIR = BASE_DIR.parent / "logs"
EMBEDDED_CPU_DIR = BASE_DIR.parent / "embedded_cpus"

# 対人プレイ用の卓状態は 1 つだけメモリに保持し、CPU 自己対戦は別ジョブで
# 実行して、長時間処理でもメイン UI が止まらないようにしています。
app = FastAPI(title="Texas Hold'em Simulator", version="0.1.0")
game = HoldemGame(LOGS_DIR, EMBEDDED_CPU_DIR)
cpu_multi_jobs: dict[str, dict] = {}
cpu_multi_jobs_lock = threading.Lock()
cfr_training_jobs: dict[str, dict] = {}
cfr_training_jobs_lock = threading.Lock()


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


class CpuMultiMatchRequest(BaseModel):
    cpu_paths: list[str]
    hands: int = 100
    starting_stack: int = 5000
    export_strategy_path: Optional[str] = None
    live_replay: bool = True


class CfrTrainingRequest(BaseModel):
    iterations: int = 20000
    starting_stack: int = 5000
    out_path: str
    base_table_path: Optional[str] = None
    min_visits: int = 25
    smoothing_alpha: float = 6.0
    seed: int = 7
    progress_every: int = 1000


def sanitize_upload_name(filename: str, default_suffix: str = ".py") -> str:
    # アップロードした CPU ファイルはディスクに保存するため、OS 差分に
    # 影響されにくい安全なファイル名へ正規化します。
    source = Path(filename or f"uploaded_file{default_suffix}")
    stem = source.stem
    suffix = source.suffix.lower() or default_suffix
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "uploaded_cpu"
    return f"{safe}{suffix}"


def seat_bundle_slug(seat: int) -> str:
    player = game.players[seat]
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", player.name.strip().lower()).strip("_") or f"seat_{seat}"
    return f"bundle_{safe_name}"


def next_seat_bundle_dir(seat: int) -> Path:
    prefix = seat_bundle_slug(seat)
    existing = sorted(EMBEDDED_CPU_DIR.glob(f"{prefix}_*"))
    highest = 0
    for path in existing:
        suffix = path.name.removeprefix(f"{prefix}_")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return EMBEDDED_CPU_DIR / f"{prefix}_{highest + 1:04d}"


def next_multi_bundle_dir() -> Path:
    prefix = "bundle_cpu_multi"
    existing = sorted(EMBEDDED_CPU_DIR.glob(f"{prefix}_*"))
    highest = 0
    for path in existing:
        suffix = path.name.removeprefix(f"{prefix}_")
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return EMBEDDED_CPU_DIR / f"{prefix}_{highest + 1:04d}"


def resolve_upload_bundle_dir(seat: Optional[int], cpu_filename: str) -> Path:
    if seat is None:
        return next_multi_bundle_dir()

    player = game.players[seat]
    current_path = Path(player.cpu_path).resolve() if player.cpu_path else None
    if current_path and current_path.exists():
        try:
            current_path.relative_to(EMBEDDED_CPU_DIR.resolve())
            current_dir = current_path.parent
            if current_dir.is_dir() and current_path.name == cpu_filename:
                return current_dir
        except ValueError:
            pass

    return next_seat_bundle_dir(seat)


async def save_uploaded_cpu(file: UploadFile) -> Path:
    saved_path, _saved_strategy_path = await save_uploaded_cpu_bundle(file)
    return saved_path


async def save_uploaded_cpu_bundle(
    cpu_file: UploadFile,
    strategy_file: Optional[UploadFile] = None,
    seat: Optional[int] = None,
) -> tuple[Path, Optional[Path]]:
    cpu_filename = sanitize_upload_name(cpu_file.filename or "uploaded_cpu.py", ".py")
    if not cpu_filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="CPU file must be a .py file.")

    cpu_content = await cpu_file.read()
    if not cpu_content:
        raise HTTPException(status_code=400, detail="Uploaded CPU file is empty.")

    strategy_filename = None
    strategy_content = None
    if strategy_file and strategy_file.filename:
        strategy_filename = sanitize_upload_name(strategy_file.filename, ".json")
        if not strategy_filename.endswith(".json"):
            strategy_filename = f"{Path(strategy_filename).stem}.json"
        strategy_content = await strategy_file.read()
        if not strategy_content:
            raise HTTPException(status_code=400, detail="Uploaded strategy JSON is empty.")

    bundle_dir = resolve_upload_bundle_dir(seat, cpu_filename)
    bundle_dir.mkdir(parents=True, exist_ok=True)

    cpu_target = bundle_dir / cpu_filename
    cpu_target.write_bytes(cpu_content)

    strategy_target: Optional[Path] = None
    if strategy_filename and strategy_content is not None:
        strategy_target = bundle_dir / strategy_filename
        strategy_target.write_bytes(strategy_content)

    return cpu_target, strategy_target


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def get_state(reveal_folded: bool = Query(False)) -> dict:
    return game.serialize_state(reveal_folded=reveal_folded)


@app.post("/api/new-hand")
async def new_hand() -> dict:
    if not game.awaiting_new_hand:
        raise HTTPException(
            status_code=400,
            detail="このハンドはまだ進行中です。アクションを完了してから次のゲームへ進んでください。",
        )
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


@app.post("/api/upload-cpu-file")
async def upload_cpu_file(
    file: UploadFile = File(...),
    strategy_file: Optional[UploadFile] = File(None),
    seat: Optional[int] = Form(None),
) -> dict:
    try:
        # まず保存しておくと、同じアップロード結果を対局用・自己対戦用・
        # 戦略表生成用で使い回せます。
        saved_path, saved_strategy_path = await save_uploaded_cpu_bundle(file, strategy_file, seat)
        if seat is not None:
            game.load_cpu(seat, str(saved_path))
            state = game.serialize_state()
            state["uploaded_cpu_path"] = str(saved_path)
            state["uploaded_strategy_path"] = str(saved_strategy_path) if saved_strategy_path else None
            return state
        return {
            "uploaded_cpu_path": str(saved_path),
            "uploaded_strategy_path": str(saved_strategy_path) if saved_strategy_path else None,
        }
    except HTTPException:
        raise
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
            capture_replay=request.live_replay,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/start-cpu-multiplayer")
async def start_cpu_multiplayer(request: CpuMultiMatchRequest) -> dict:
    job_id = uuid.uuid4().hex
    with cpu_multi_jobs_lock:
        cpu_multi_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "completed_hands": 0,
            "total_hands": request.hands,
            "percent": 0.0,
            "message": "Waiting to start.",
            "latest_snapshot": None,
            "leaderboard_preview": [],
            "result": None,
            "error": None,
            "started_at": time.time(),
            "elapsed_seconds": 0.0,
            "estimated_remaining_seconds": None,
            "capture_replay": request.live_replay and request.hands <= 10000,
        }

    def progress_callback(payload: dict) -> None:
        # ワーカースレッドはここへ軽い進捗情報だけを書き込み、UI 側は
        # ポーリングで進捗バーやライブ再生を更新します。
        with cpu_multi_jobs_lock:
            job = cpu_multi_jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
            job["completed_hands"] = payload.get("completed_hands", job["completed_hands"])
            job["total_hands"] = payload.get("total_hands", job["total_hands"])
            job["percent"] = payload.get("percent", job["percent"])
            job["message"] = payload.get("message", job["message"])
            job["latest_snapshot"] = payload.get("latest_snapshot")
            job["leaderboard_preview"] = payload.get("leaderboard_preview", job["leaderboard_preview"])
            job["elapsed_seconds"] = payload.get("elapsed_seconds", job["elapsed_seconds"])
            job["estimated_remaining_seconds"] = payload.get(
                "estimated_remaining_seconds",
                job["estimated_remaining_seconds"],
            )

    def worker() -> None:
        try:
            # 自己対戦は長く走ることがあるので、リクエストスレッド外で実行し、
            # 結果とスナップショットだけをジョブ領域へ返します。
            capture_replay = request.live_replay and request.hands <= 10000
            result = run_multiway_cpu_match(
                logs_dir=LOGS_DIR,
                embedded_cpu_dir=EMBEDDED_CPU_DIR,
                cpu_paths=request.cpu_paths,
                hands=request.hands,
                starting_stack=request.starting_stack,
                export_strategy_path=request.export_strategy_path,
                progress_callback=progress_callback,
                capture_replay=capture_replay,
            )
            with cpu_multi_jobs_lock:
                job = cpu_multi_jobs.get(job_id)
                if not job:
                    return
                job["status"] = "completed"
                job["percent"] = 100.0
                job["completed_hands"] = request.hands
                job["message"] = "CPU self-play finished."
                job["latest_snapshot"] = result.get("last_replay_snapshot")
                job["result"] = result
                job["elapsed_seconds"] = result.get("elapsed_seconds", job["elapsed_seconds"])
                job["estimated_remaining_seconds"] = 0.0
                job["capture_replay"] = capture_replay
        except Exception as exc:
            with cpu_multi_jobs_lock:
                job = cpu_multi_jobs.get(job_id)
                if not job:
                    return
                job["status"] = "failed"
                job["error"] = str(exc)
                job["message"] = str(exc)

    threading.Thread(target=worker, daemon=True).start()
    with cpu_multi_jobs_lock:
        job = cpu_multi_jobs[job_id]
    return {
        "job_id": job_id,
        "status": job["status"],
        "completed_hands": job["completed_hands"],
        "total_hands": job["total_hands"],
        "percent": job["percent"],
        "message": job["message"],
        "capture_replay": job["capture_replay"],
    }


@app.get("/api/cpu-multiplayer-jobs/{job_id}")
async def get_cpu_multiplayer_job(job_id: str) -> dict:
    with cpu_multi_jobs_lock:
        job = cpu_multi_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job


@app.post("/api/start-cfr-training")
async def start_cfr_training(request: CfrTrainingRequest) -> dict:
    job_id = uuid.uuid4().hex
    with cfr_training_jobs_lock:
        cfr_training_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "completed_iterations": 0,
            "total_iterations": request.iterations,
            "percent": 0.0,
            "message": "Waiting to start.",
            "result": None,
            "error": None,
            "elapsed_seconds": 0.0,
            "estimated_remaining_seconds": None,
            "infosets": 0,
            "started_at": time.time(),
        }

    def progress_callback(payload: dict) -> None:
        with cfr_training_jobs_lock:
            job = cfr_training_jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
            job["completed_iterations"] = payload.get(
                "completed_iterations",
                job["completed_iterations"],
            )
            job["total_iterations"] = payload.get("total_iterations", job["total_iterations"])
            job["percent"] = payload.get("percent", job["percent"])
            job["message"] = payload.get("message", job["message"])
            job["elapsed_seconds"] = payload.get("elapsed_seconds", job["elapsed_seconds"])
            job["estimated_remaining_seconds"] = payload.get(
                "estimated_remaining_seconds",
                job["estimated_remaining_seconds"],
            )
            job["infosets"] = payload.get("infosets", job["infosets"])

    def worker() -> None:
        try:
            result = build_and_save_strategy_table(
                iterations=request.iterations,
                starting_stack=request.starting_stack,
                out_path=request.out_path,
                seed=request.seed,
                min_visits=request.min_visits,
                smoothing_alpha=request.smoothing_alpha,
                base_table_path=request.base_table_path,
                progress_callback=progress_callback,
                progress_every=request.progress_every,
            )
            with cfr_training_jobs_lock:
                job = cfr_training_jobs.get(job_id)
                if not job:
                    return
                job["status"] = "completed"
                job["completed_iterations"] = request.iterations
                job["percent"] = 100.0
                job["message"] = "CFR training finished."
                job["result"] = result
                job["elapsed_seconds"] = result.get("elapsed_seconds", job["elapsed_seconds"])
                job["estimated_remaining_seconds"] = 0.0
                job["infosets"] = result.get("infosets", job["infosets"])
        except Exception as exc:
            with cfr_training_jobs_lock:
                job = cfr_training_jobs.get(job_id)
                if not job:
                    return
                job["status"] = "failed"
                job["error"] = str(exc)
                job["message"] = str(exc)

    threading.Thread(target=worker, daemon=True).start()
    with cfr_training_jobs_lock:
        job = cfr_training_jobs[job_id]
    return {
        "job_id": job_id,
        "status": job["status"],
        "completed_iterations": job["completed_iterations"],
        "total_iterations": job["total_iterations"],
        "percent": job["percent"],
        "message": job["message"],
    }


@app.get("/api/cfr-training-jobs/{job_id}")
async def get_cfr_training_job(job_id: str) -> dict:
    with cfr_training_jobs_lock:
        job = cfr_training_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
