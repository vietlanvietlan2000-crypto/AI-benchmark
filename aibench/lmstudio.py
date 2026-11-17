"""Kết nối LM Studio: API tương thích OpenAI (đo tốc độ khi stream) + lệnh `lms` (nạp/gỡ model)."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class GenStats:
    """Kết quả và số đo của 1 lần sinh văn bản."""

    text: str = ""
    reasoning: str = ""  # phần "suy nghĩ" của model reasoning (gpt-oss, qwen...)
    ttft_s: float = 0.0  # thời gian tới token đầu tiên (gồm thời gian đọc prompt)
    total_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str | None = None

    @property
    def gen_tps(self) -> float:
        """Tốc độ sinh (token/giây), tính sau token đầu tiên."""
        dt = self.total_s - self.ttft_s
        return (self.completion_tokens - 1) / dt if self.completion_tokens > 1 and dt > 0 else 0.0

    @property
    def prompt_tps(self) -> float:
        """Tốc độ đọc prompt (token/giây), xấp xỉ bằng prompt_tokens / TTFT."""
        return self.prompt_tokens / self.ttft_s if self.ttft_s > 0 else 0.0


def _lms_exe() -> str:
    exe = shutil.which("lms")
    if exe:
        return exe
    fallback = Path.home() / ".lmstudio" / "bin" / "lms.exe"
    if fallback.is_file():
        return str(fallback)
    raise RuntimeError("Không tìm thấy lệnh 'lms' của LM Studio")


async def _lms(*args: str, timeout: float = 600) -> str:
    def call() -> str:
        r = subprocess.run(
            [_lms_exe(), *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
        )
        if r.returncode != 0:
            raise RuntimeError(f"lms {' '.join(args)} lỗi: {(r.stderr or r.stdout).strip()[-500:]}")
        return r.stdout

    return await asyncio.to_thread(call)


class LMStudio:
    def __init__(self, base_url: str = "http://localhost:1234"):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5))

    async def close(self) -> None:
        await self._http.aclose()

    # ---- điều khiển LM Studio -------------------------------------------------
    async def is_up(self) -> bool:
        try:
            r = await self._http.get(f"{self.base_url}/v1/models", timeout=3)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    @staticmethod
    async def start_server() -> None:
        await _lms("server", "start", timeout=60)

    @staticmethod
    async def load(model: str) -> None:
        await _lms("load", model, "-y")

    @staticmethod
    async def unload_all() -> None:
        await _lms("unload", "--all", timeout=120)

    async def list_models(self) -> list[dict[str, Any]]:
        """Danh sách model. Mỗi phần tử có id, type (llm/vlm/embeddings), state (loaded/not-loaded)..."""
        r = await self._http.get(f"{self.base_url}/api/v0/models")
        if r.status_code == 200:
            data = r.json().get("data", [])
            # Dung lượng file model (để ước lượng model có vừa VRAM không) chỉ có ở API v1.
            try:
                r1 = await self._http.get(f"{self.base_url}/api/v1/models", timeout=5)
                extra = {m.get("key"): m for m in r1.json().get("models", [])} if r1.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                extra = {}
            for m in data:
                e = extra.get(m.get("id")) or {}
                if e.get("size_bytes"):
                    m["size_bytes"] = e["size_bytes"]
            return data
        r = await self._http.get(f"{self.base_url}/v1/models")
        r.raise_for_status()
        return [{"id": m["id"], "type": "llm", "state": "unknown"} for m in r.json().get("data", [])]

    # ---- sinh văn bản ---------------------------------------------------------
    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 256,
        temperature: float = 0.0,
        should_stop: Callable[[GenStats], bool] | None = None,
        **extra: Any,
    ) -> GenStats:
        """Gọi chat completion dạng stream và đo TTFT, tốc độ sinh.

        `messages` theo chuẩn OpenAI nên dùng được cả ảnh (image_url) cho model thị giác.
        `should_stop` được gọi sau mỗi 100 mẩu; trả True thì ngắt giữa chừng (finish_reason = "aborted"),
        dùng để chặn model lặp vòng khi không đặt giới hạn token.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            **extra,
        }
        st = GenStats()
        chunks = 0
        t0 = time.perf_counter()
        first: float | None = None
        async with self._http.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"LM Studio trả lỗi {r.status_code}: {body[:500]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                if obj.get("error"):
                    raise RuntimeError(f"LM Studio báo lỗi: {obj['error']}")
                if obj.get("usage"):
                    st.prompt_tokens = obj["usage"].get("prompt_tokens", 0)
                    st.completion_tokens = obj["usage"].get("completion_tokens", 0)
                for ch in obj.get("choices") or []:
                    delta = ch.get("delta") or {}
                    piece = delta.get("content") or ""
                    think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if piece or think:
                        if first is None:
                            first = time.perf_counter()
                        chunks += 1
                        st.text += piece
                        st.reasoning += think if isinstance(think, str) else ""
                    if ch.get("finish_reason"):
                        st.finish_reason = ch["finish_reason"]
                if should_stop and chunks and chunks % 100 == 0 and not st.finish_reason and should_stop(st):
                    st.finish_reason = "aborted"
                    break  # ra khỏi `async with` là đóng kết nối, LM Studio dừng sinh
        end = time.perf_counter()
        st.total_s = end - t0
        st.ttft_s = (first or end) - t0
        if not st.completion_tokens:  # server không trả usage → ước lượng theo số chunk
            st.completion_tokens = chunks
        return st
