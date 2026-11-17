"""API của chatbot hỏi đáp tài liệu. Phần tìm tài liệu nằm ở rag.py."""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import rag
from .core import LM_URL
from .lmstudio import LMStudio, _lms

router = APIRouter(prefix="/api/chat")
busy: Callable[[], Any] = lambda: None  # server.py gán: trả job benchmark đang chạy (nếu có)
indexing: dict[str, Any] = {"running": False, "done": 0, "total": 0, "file": "", "error": None}
_index: rag.Index | None = None

CTX_WANT = 16384  # ngữ cảnh khi chatbot tự nạp model (đủ để đọc trọn tài liệu vài trang)
EMBED_CTX = 4096  # ngữ cảnh của model embedding: mỗi đoạn tài liệu chỉ dài khoảng 1.200 ký tự
SYSTEM = (
    "Bạn là trợ lý tra cứu tài liệu nội bộ. Chỉ trả lời dựa trên phần TÀI LIỆU được cung cấp trong tin nhắn. "
    "Nếu tài liệu không có thông tin cần hỏi, hãy nói rõ là không tìm thấy trong tài liệu, không tự bịa. "
    "Cuối câu trả lời ghi tên tài liệu đã dùng. Trả lời ngắn gọn, bằng ngôn ngữ của câu hỏi."
)
SYSTEM_PLAIN = "Bạn là trợ lý AI hữu ích. Trả lời ngắn gọn, bằng ngôn ngữ của câu hỏi."


def index() -> rag.Index:
    global _index
    if _index is None:
        _index = rag.Index.load()
    return _index


def _check_idle() -> None:
    if busy():
        raise HTTPException(409, "Đang có một bài test chạy, hãy đợi hoặc huỷ nó trước")


@router.get("/state")
async def state() -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ix = index()
        stale = set(ix.stale())
        files = []
        for p in rag.list_files():
            r = rag.rel(p)
            f = ix.files.get(r) or {}
            files.append({"path": r, "kb": round(p.stat().st_size / 1024, 1), "indexed": r in ix.files and r not in stale,
                          "chunks": f.get("n", 0), "note": f.get("note", "")})
        return {"docs_dir": str(rag.DOCS), "exts": sorted(rag.EXTS), "files": files, "embed_model": ix.model,
                "chunks": len(ix.chunks), "indexing": indexing}

    return await asyncio.to_thread(work)


@router.post("/index")
async def build_index(body: dict[str, Any]) -> dict[str, Any]:
    _check_idle()
    model = str(body.get("embed_model") or "")
    if not model:
        raise HTTPException(400, "Chưa chọn model embedding")
    if indexing["running"]:
        return {"ok": True}
    indexing.update(running=True, done=0, total=0, file="", error=None)

    async def work() -> None:
        global _index
        try:
            await _prepare_embed(model)
            _index = await rag.build(model, indexing)
        except Exception as e:  # noqa: BLE001
            indexing["error"] = str(e) or type(e).__name__
        finally:
            indexing["running"] = False

    asyncio.get_running_loop().create_task(work())
    return {"ok": True}


@router.post("/open")
async def open_doc(body: dict[str, Any]) -> dict[str, Any]:
    """Mở thư mục tài liệu (path rỗng) hoặc 1 tài liệu trong đó."""
    rag.DOCS.mkdir(exist_ok=True)
    path = (rag.DOCS / str(body.get("path", ""))).resolve()
    if not path.exists() or not (path == rag.DOCS or path.is_relative_to(rag.DOCS)):
        raise HTTPException(400, "Không mở được file này")
    os.startfile(path)  # noqa: S606
    return {"ok": True}


async def _prepare_embed(model: str) -> None:
    """Nạp model embedding với ngữ cảnh nhỏ. Để LM Studio tự nạp thì nó dùng ngữ cảnh tối đa của model
    (vd. 40.960 token với Qwen3 Embedding 8B), tốn hơn 10 GB VRAM và không còn chỗ cho model trả lời."""
    lm = LMStudio(LM_URL)
    try:
        info = next((m for m in await lm.list_models() if m["id"] == model), None)
    finally:
        await lm.close()
    if info is None:
        raise RuntimeError(f"LM Studio không có model {model}")
    if info.get("state") != "loaded":
        try:
            await _lms("load", model, "-y", "-c", str(min(EMBED_CTX, info.get("max_context_length") or EMBED_CTX)))
        except RuntimeError as e:
            raise RuntimeError(f"Không nạp được model embedding {model}. Có thể VRAM đã đầy: hãy bấm \"Gỡ các model đã nạp\" "
                               f"hoặc chọn model embedding nhỏ hơn. ({e})") from e


async def _prepare_model(model: str) -> int:
    """Bảo đảm model chat đã nạp; trả về độ dài ngữ cảnh (token) đang dùng."""
    lm = LMStudio(LM_URL)
    try:
        models = await lm.list_models()
        info = next((m for m in models if m["id"] == model), None)
        if info is None:
            raise RuntimeError(f"LM Studio không có model {model}")
        if info.get("state") != "loaded":
            for m in models:  # gỡ model chat khác để nhường VRAM, giữ lại model embedding
                if m.get("state") == "loaded" and m.get("type") != "embeddings":
                    await _lms("unload", m["id"], timeout=120)
            ctx = min(CTX_WANT, info.get("max_context_length") or CTX_WANT)
            await _lms("load", model, "-y", "-c", str(ctx))
            info = next((m for m in await lm.list_models() if m["id"] == model), info)
        return int(info.get("loaded_context_length") or 4096)
    finally:
        await lm.close()


@router.post("/ask")
async def ask(body: dict[str, Any]) -> StreamingResponse:
    _check_idle()
    model = str(body.get("model") or "")
    msgs = [m for m in body.get("messages") or [] if m.get("role") in ("user", "assistant") and m.get("content")]
    if not model or not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "Thiếu model hoặc câu hỏi")
    use_docs = bool(body.get("use_docs", True))

    async def gen() -> AsyncIterator[str]:
        def ev(**k: Any) -> str:
            return json.dumps(k, ensure_ascii=False) + "\n"

        try:
            yield ev(type="status", text="Đang nạp model…")
            ctx = await _prepare_model(model)
            history, question = msgs[-7:-1], msgs[-1]["content"]
            sources: list[dict[str, Any]] = []
            content = question
            if use_docs:
                ix = index()
                if not ix.chunks:
                    raise RuntimeError("Chưa có chỉ mục tài liệu. Hãy bấm \"Cập nhật chỉ mục\" trước.")
                yield ev(type="status", text="Đang tìm tài liệu liên quan…")
                await _prepare_embed(ix.model)
                used = sum(len(m["content"]) for m in history) + len(question)
                budget = max(1500, min(40000, int((ctx - 1500) * 2.2) - used))
                prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
                docs, sources = await rag.retrieve(ix, f"{prev}\n{question}".strip(), budget)  # kèm câu trước để hiểu câu hỏi nối tiếp
                content = f"TÀI LIỆU:\n{docs}\n\nCÂU HỎI: {question}"
            yield ev(type="sources", sources=sources, ctx=ctx)

            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": -1, "stream_options": {"include_usage": True},
                       "messages": [{"role": "system", "content": SYSTEM if use_docs else SYSTEM_PLAIN}, *history,
                                    {"role": "user", "content": content}]}
            t0, first, tokens, thinking = time.perf_counter(), None, 0, False
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as http, \
                    http.stream("POST", f"{LM_URL}/v1/chat/completions", json=payload) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"LM Studio trả lỗi {r.status_code}: {(await r.aread()).decode('utf-8', 'replace')[:300]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                        continue
                    obj = json.loads(line[5:])
                    if obj.get("error"):
                        raise RuntimeError(f"LM Studio báo lỗi: {obj['error']}")
                    if obj.get("usage"):
                        tokens = obj["usage"].get("completion_tokens", 0)
                    for ch in obj.get("choices") or []:
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            first = first or time.perf_counter()
                            yield ev(type="text", text=d["content"])
                        elif (d.get("reasoning_content") or d.get("reasoning")) and not thinking:
                            thinking = True
                            yield ev(type="status", text="Model đang suy nghĩ…")
            total = time.perf_counter() - t0
            yield ev(type="done", seconds=round(total, 1), tokens=tokens, tps=round(tokens / total, 1) if total > 0 else 0)
        except Exception as e:  # noqa: BLE001
            yield ev(type="error", text=str(e) or type(e).__name__)

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
