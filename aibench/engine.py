"""Phát hiện LM Studio đang chạy model bằng GPU hay CPU (Windows).

LM Studio chạy mỗi model GGUF trong 1 tiến trình `llama-server.exe`. Từ tiến trình đó ta biết:
  - backend (CUDA / Vulkan / chỉ CPU)  ← tên thư mục backend trong đường dẫn file exe
  - số lớp đưa lên GPU, độ dài ngữ cảnh ← tham số dòng lệnh --n-gpu-layers, --ctx-size
  - VRAM thật (dedicated) và phần TRÀN sang RAM (shared) ← bộ đếm "GPU Process Memory" của Windows

"Tràn" là trường hợp hay gặp nhất khiến GPU chạy chậm như CPU: model + ngữ cảnh lớn hơn VRAM,
driver NVIDIA lặng lẽ đẩy phần dư sang RAM hệ thống thay vì báo lỗi.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_MB = 1024 * 1024
SPILL_MB = 1024  # shared > mức này coi là tràn VRAM (tiến trình bình thường chỉ dùng vài chục MB)
TTL_S = 15  # hỏi Windows tốn 2-3 giây nên chỉ hỏi lại sau mỗi khoảng này

# Hỏi 2 bộ đếm bằng 2 lệnh riêng để không phải dò theo tên (tên bộ đếm đổi theo ngôn ngữ Windows).
_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ps = @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'")
function Mem($counter) {
  $m = @{}
  (Get-Counter $counter).CounterSamples | ForEach-Object {
    if ($_.InstanceName -match '^pid_(\d+)_') { $m[$Matches[1]] = [double]$m[$Matches[1]] + $_.CookedValue }
  }
  $m
}
$ded = @{}; $shr = @{}
if ($ps.Count) { $ded = Mem '\GPU Process Memory(*)\Dedicated Usage'; $shr = Mem '\GPU Process Memory(*)\Shared Usage' }
$out = @($ps | ForEach-Object {
  $id = "$($_.ProcessId)"
  [pscustomobject]@{ cmd = $_.CommandLine; ram = [double]$_.WorkingSetSize; ded = $ded[$id]; shr = $shr[$id] }
})
ConvertTo-Json -InputObject $out -Compress
"""


def ram_total_mb() -> int | None:
    """Tổng RAM của máy (MB), dùng để ước lượng model có chạy nổi không. None nếu không hỏi được."""
    if sys.platform != "win32":
        return None
    import ctypes

    class _Mem(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong) for n in ("total", "avail", "page_total", "page_avail", "virt_total", "virt_avail", "ext")]

    st = _Mem()
    st.length = ctypes.sizeof(st)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    except (OSError, AttributeError):
        return None
    return round(st.total / _MB) if ok else None


def _arg(cmd: str, name: str) -> str | None:
    m = re.search(rf'{re.escape(name)}\s+(?:"([^"]*)"|(\S+))', cmd)
    return (m.group(1) or m.group(2)) if m else None


def _mb(v: Any) -> int | None:
    return None if v is None else round(float(v) / _MB)


def _describe(p: dict[str, Any]) -> dict[str, Any]:
    cmd = p.get("cmd") or ""
    low = cmd.lower()
    backend = next((name for key, name in (("cuda", "CUDA"), ("vulkan", "Vulkan"), ("rocm", "ROCm"), ("opencl", "OpenCL"))
                    if key in low.split("llama-server.exe")[0]), "CPU")
    layers = _arg(cmd, "--n-gpu-layers")
    ctx = _arg(cmd, "--ctx-size")
    model = _arg(cmd, "--model")
    vram, shared = _mb(p.get("ded")), _mb(p.get("shr"))
    n_layers = int(layers) if layers and re.fullmatch(r"-?\d+", layers) else None

    if backend == "CPU" or n_layers == 0 or (vram is not None and vram < 300):
        mode = "cpu"
    elif shared is not None and shared > SPILL_MB:
        mode = "spill"
    else:
        mode = "gpu"
    # Chỉ trả về các trường đã tách; KHÔNG trả nguyên dòng lệnh vì trong đó có --api-key.
    return {"mode": mode, "backend": backend, "model": Path(model).stem if model else None, "gpu_layers": n_layers,
            "ctx": int(ctx) if ctx and ctx.isdigit() else None, "vram_mb": vram, "shared_mb": shared,
            "ram_mb": _mb(p.get("ram"))}


def detect() -> dict[str, Any]:
    """Trả {"mode": "gpu"|"spill"|"cpu"|"idle"|"unknown", ...} cho model đang nạp (model lớn nhất nếu có nhiều)."""
    if sys.platform != "win32":
        return {"mode": "unknown"}
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
                             capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW).stdout.strip()
        procs = json.loads(out) if out else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"mode": "unknown"}
    if isinstance(procs, dict):
        procs = [procs]
    engines = [_describe(p) for p in procs if isinstance(p, dict)]
    if not engines:
        return {"mode": "idle"}
    return max(engines, key=lambda e: (e["vram_mb"] or 0) + (e["shared_mb"] or 0) + (e["ram_mb"] or 0))


_cache: dict[str, Any] = {"t": 0.0, "v": {"mode": "unknown"}}
_lock = asyncio.Lock()


async def engine_status(fresh: bool = False) -> dict[str, Any]:
    """Như detect() nhưng có nhớ tạm, dùng cho /api/status (được hỏi vài giây 1 lần)."""
    async with _lock:
        if fresh or time.monotonic() - _cache["t"] > TTL_S:
            _cache["v"] = await asyncio.to_thread(detect)
            _cache["t"] = time.monotonic()
        return _cache["v"]
