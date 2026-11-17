"""Web server: API + giao diện. Chạy: python -m aibench serve"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .core import GPU_INDEX, LM_URL, ROOT, list_results, load_result, modules, run_module
from .engine import engine_status, ram_total_mb
from .gpu import gpu_info
from . import setup as st
from .lmstudio import LMStudio

app = FastAPI(title="AI Bench")
WEB = Path(__file__).parent / "web"

# Chỉ cho chạy 1 job tại 1 thời điểm để các phép đo GPU không ảnh hưởng nhau.
jobs: dict[str, dict[str, Any]] = {}
tasks: dict[str, asyncio.Task] = {}
job_images: dict[str, list[dict[str, Any]]] = {}


def _running() -> dict[str, Any] | None:
    return next((j for j in jobs.values() if j["status"] == "running"), None)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/logo.png")
async def logo() -> FileResponse:
    return FileResponse(WEB / "logo.png")


@app.get("/logo-mark.png")
async def logo_mark() -> FileResponse:
    return FileResponse(WEB / "logo-mark.png")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    lm = LMStudio(LM_URL)
    try:
        up = await lm.is_up()
        models = await lm.list_models() if up else []
    finally:
        await lm.close()
    running = _running()
    return {
        "lmstudio": {"url": LM_URL, "up": up},
        "models": models,
        "gpu": await asyncio.to_thread(gpu_info, GPU_INDEX),
        "engine": await engine_status(),
        "ram_total_mb": ram_total_mb(),
        "running_job": running["id"] if running else None,
    }


@app.post("/api/lmstudio/start")
async def start_lmstudio() -> dict[str, Any]:
    try:
        await LMStudio.start_server()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e
    return {"ok": True}


# ---- thư mục model của LM Studio ---------------------------------------------------------------
def _count_models(folder: Path) -> int:
    """Số model trong thư mục (bố cục LM Studio: <nhà phát hành>/<model>/*.gguf; không tính file mmproj)."""
    try:
        return sum(1 for f in folder.glob("*/*/*.gguf") if not f.name.lower().startswith("mmproj"))
    except OSError:
        return 0


def _model_dirs() -> dict[str, Any]:
    cur = st.models_dir()
    cands = [st.LMS_HOME / "models", cur, *(Path(root) / "LM Studio Models" for root, _f, _t in st.drives())]
    seen: dict[str, Path] = {}
    for p in cands:
        if p == cur or p.is_dir():
            seen.setdefault(str(p).lower().rstrip("\\/"), p)
    key = str(cur).lower().rstrip("\\/")
    return {"current": str(cur), "dirs": [
        {"path": str(p), "models": _count_models(p), "free_gb": round(st.free_gb(p)), "current": k == key,
         "default": p == st.LMS_HOME / "models"} for k, p in seen.items()]}


@app.get("/api/model-dirs")
async def model_dirs() -> dict[str, Any]:
    return await asyncio.to_thread(_model_dirs)


async def _lm_ids() -> set[str] | None:
    lm = LMStudio(LM_URL)
    try:
        return {m["id"] for m in await lm.list_models()} if await lm.is_up() else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        await lm.close()


@app.post("/api/model-dir")
async def set_model_dir(body: dict[str, Any]) -> dict[str, Any]:
    """Đổi thư mục model của LM Studio. restart=True: tắt hẳn LM Studio, ghi cài đặt rồi mở lại."""
    if _running():
        raise HTTPException(409, "Đang có một bài test chạy, hãy đợi hoặc huỷ nó trước")
    path = Path(str(body.get("path", "")).strip().strip('"'))
    if not path.is_absolute():
        raise HTTPException(400, "Đường dẫn thư mục không hợp lệ")
    before = await _lm_ids()
    if body.get("restart"):
        await asyncio.to_thread(subprocess.run, ["taskkill", "/F", "/T", "/IM", "LM Studio.exe"], capture_output=True)
        await asyncio.sleep(2)
    if not await asyncio.to_thread(st.set_models_dir, path):
        raise HTTPException(500, "Không ghi được cài đặt của LM Studio")
    if body.get("restart"):
        exe = st.lmstudio_app()
        if exe:
            os.startfile(exe)  # noqa: S606
        for _ in range(30):  # chờ LM Studio mở xong rồi bật server
            await asyncio.sleep(2)
            try:
                await LMStudio.start_server()
            except Exception:  # noqa: BLE001
                continue
            if await _lm_ids() is not None:
                return {"ok": True, "applied": True}
        raise HTTPException(500, "LM Studio chưa mở lại được, hãy mở LM Studio bằng tay")
    if before is None:  # LM Studio đang tắt: lần mở tới sẽ dùng thư mục mới
        return {"ok": True, "applied": True}
    for _ in range(4):  # LM Studio đang chạy: xem nó có tự nhận thư mục mới không
        await asyncio.sleep(2)
        if await _lm_ids() != before:
            return {"ok": True, "applied": True}
    return {"ok": True, "applied": False}


@app.get("/api/modules")
async def get_modules() -> list[dict[str, Any]]:
    return [m.describe() for m in modules().values()]


@app.post("/api/jobs")
async def create_job(body: dict[str, Any]) -> dict[str, Any]:
    if _running():
        raise HTTPException(409, "Đang có một bài test chạy, hãy đợi hoặc huỷ nó trước")
    mod = modules().get(body.get("module", ""))
    if mod is None:
        raise HTTPException(404, "Không có module này")
    try:
        mod.coerce(body.get("params") or {})
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e

    job = {"id": uuid.uuid4().hex[:12], "module": mod.id, "status": "running", "progress": 0.0,
           "model": (body.get("params") or {}).get("model"),
           "log": [], "result_id": None, "error": None, "images_ver": 0}
    jobs[job["id"]] = job
    images: list[dict[str, Any]] = []
    job_images[job["id"]] = images

    def show(item: dict[str, Any]) -> None:
        if images and images[-1].get("key") == item.get("key"):
            images[-1] = item
        else:
            images.append(item)
        job["images_ver"] += 1

    t0 = time.perf_counter()

    def log(msg: str) -> None:
        job["log"].append(f"[{time.perf_counter() - t0:6.1f}s] {msg}")

    def progress(f: float) -> None:
        job["progress"] = round(f, 3)

    async def runner() -> None:
        try:
            record = await run_module(mod.id, body.get("params") or {}, log, progress, show)
            job["result_id"] = record["id"]
            job["status"] = "done"
            log("Hoàn tất")
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            log("Đã huỷ")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            job["status"] = "error"
            job["error"] = str(e) or type(e).__name__
            log(f"LỖI: {job['error']}")

    tasks[job["id"]] = asyncio.create_task(runner())
    return job


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    if job_id not in jobs:
        raise HTTPException(404, "Không có job này")
    return jobs[job_id]


@app.get("/api/jobs/{job_id}/images")
async def get_job_images(job_id: str) -> list[dict[str, Any]]:
    """Ảnh module đã đưa lên trong lúc chạy (tách riêng để việc hỏi tiến trình luôn nhẹ)."""
    return job_images.get(job_id, [])


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    task = tasks.get(job_id)
    if task and not task.done():
        task.cancel()
    return {"ok": True}


@app.post("/api/open")
async def open_file(body: dict[str, Any]) -> dict[str, Any]:
    """Mở 1 file kết quả bằng ứng dụng mặc định (vd. Excel). Chỉ cho mở file nằm trong thư mục dự án."""
    path = Path(str(body.get("path", ""))).resolve()
    if not path.is_file() or not path.is_relative_to(ROOT) or path.suffix.lower() not in (".xlsx", ".csv", ".json", ".txt"):
        raise HTTPException(400, "Không mở được file này")
    os.startfile(path)  # noqa: S606 - chỉ có trên Windows
    return {"ok": True}


@app.get("/api/results")
async def results() -> list[dict[str, Any]]:
    return list_results()


@app.get("/api/results/{result_id}")
async def result(result_id: str) -> dict[str, Any]:
    r = load_result(result_id)
    if r is None:
        raise HTTPException(404, "Không có kết quả này")
    return r
