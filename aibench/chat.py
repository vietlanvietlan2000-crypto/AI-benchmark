"""API của chatbot hỏi đáp tài liệu. Phần tìm tài liệu nằm ở rag.py."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse

from . import rag, websearch
from .core import LM_URL
from .lmstudio import LMStudio, _lms

router = APIRouter(prefix="/api/chat")
busy: Callable[[], Any] = lambda: None  # server.py gán: trả job benchmark đang chạy (nếu có)
indexing: dict[str, Any] = {"running": False, "done": 0, "total": 0, "file": "", "error": None}
_index: rag.Index | None = None

CTX_WANT = 16384  # ngữ cảnh khi chatbot tự nạp model (đủ để đọc trọn tài liệu vài trang)
EMBED_CTX = 4096  # ngữ cảnh của model embedding: mỗi đoạn tài liệu chỉ dài khoảng 1.200 ký tự
SYSTEM = (
    "Bạn là trợ lý tra cứu. Chỉ trả lời dựa trên các NGUỒN THAM KHẢO được đánh số trong tin nhắn. "
    "Sau mỗi ý, ghi số của nguồn đã dùng trong ngoặc vuông, ví dụ [2] hoặc [1][3]. "
    "Nếu các nguồn không có thông tin cần hỏi, hãy nói rõ là không tìm thấy, không tự bịa. "
    "Trả lời ngắn gọn, bằng ngôn ngữ của câu hỏi."
)
SYSTEM_PLAIN = "Bạn là trợ lý AI hữu ích. Trả lời ngắn gọn, bằng ngôn ngữ của câu hỏi."
FORMAT = (
    " Khi người dùng muốn bảng số liệu hoặc file Excel: trình bày số liệu bằng bảng Markdown (giao diện có sẵn nút xuất Excel "
    "cho mỗi bảng, đừng nói rằng bạn không tạo được file). Khi người dùng muốn biểu đồ: viết một khối mã ```chart chứa JSON dạng "
    '{"type": "bar", "title": "...", "labels": ["A", "B"], "series": [{"name": "...", "data": [1, 2]}]} '
    '("type" là "bar", "line" hoặc "pie"; "data" là số thuần, không có dấu phân cách hàng nghìn hay đơn vị).'
)


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


@router.get("/doc")
async def get_doc(path: str) -> dict[str, Any]:
    """Nội dung 1 tài liệu theo từng đoạn đã lập chỉ mục, để giao diện hiện và tô sáng đoạn được trích dẫn."""
    chunks = index().of(path)
    if not chunks:
        raise HTTPException(404, "Tài liệu này chưa có trong chỉ mục")
    return {"path": path, "chunks": [{"i": c["i"], "loc": c.get("loc"), "text": c["text"]} for c in chunks]}


def _num(v: Any) -> Any:
    """Chữ trông như số (kiểu Việt: 6.490.000 hoặc 5,5) thì đổi thành số để Excel tính toán được."""
    if not isinstance(v, str):
        return v
    t = v.strip().replace("\u00a0", "")
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", t):
        return int(t.replace(".", ""))
    if re.fullmatch(r"-?\d+", t) and (len(t) < 2 or t.lstrip("-")[0] != "0"):
        return int(t)
    if re.fullmatch(r"-?\d+,\d+", t):
        return float(t.replace(",", "."))
    return v


@router.post("/excel")
async def excel(body: dict[str, Any]) -> Response:
    """Tạo file Excel từ 1 bảng (rows) hoặc 1 biểu đồ (chart: kèm biểu đồ Excel thật)."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    chart = body.get("chart")
    if chart:
        series = chart.get("series") or []
        rows = [["", *[str(x.get("name") or f"Dãy {k + 1}") for k, x in enumerate(series)]]]
        for i, lab in enumerate(chart.get("labels") or []):
            rows.append([lab, *[(x.get("data") or [None] * (i + 1))[i] if i < len(x.get("data") or []) else None for x in series]])
    else:
        rows = body.get("rows") or []
    if len(rows) < 2:
        raise HTTPException(400, "Không có dữ liệu để xuất")
    wb = Workbook()
    ws = wb.active
    ws.title = "Du lieu"
    for r in rows:
        ws.append([_num(c) for c in r])
    for c in ws[1]:
        c.font = Font(bold=True)
    for k in range(1, ws.max_column + 1):
        width = max(len(str(r[k - 1])) if k <= len(r) and r[k - 1] is not None else 0 for r in rows)
        ws.column_dimensions[get_column_letter(k)].width = min(60, max(10, width + 2))
    if chart:
        kind = chart.get("type")
        ch = PieChart() if kind == "pie" else LineChart() if kind == "line" else BarChart()
        ch.title = chart.get("title") or None
        ch.add_data(Reference(ws, min_col=2, max_col=ws.max_column, min_row=1, max_row=ws.max_row), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
        ch.width, ch.height = 20, 11
        ws.add_chart(ch, f"{get_column_letter(ws.max_column + 2)}2")
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


async def _web_passages(question: str, budget: int) -> list[dict[str, Any]]:
    """Tìm trên internet và chọn những đoạn trùng nhiều từ với câu hỏi nhất ở mỗi trang."""
    qw = rag.words(question)
    out: list[dict[str, Any]] = []
    results = await websearch.gather(question)
    per_page = max(600, budget // max(1, len(results)))
    for r in results:
        parts = rag.split(r["text"]) if r["text"] else []
        parts.sort(key=lambda t: -len(qw & rag.words(t)))  # sort ổn định: bằng điểm thì đoạn đầu trang đứng trước
        picked, used = [], 0
        for t in parts[:3]:
            if used + len(t) > per_page:
                break
            picked.append(t)
            used += len(t)
        if not picked and r["snippet"]:
            picked = [r["snippet"]]
        out += [{"kind": "web", "url": r["url"], "title": r["title"], "text": t} for t in picked]
    return out


@router.post("/ask")
async def ask(body: dict[str, Any]) -> StreamingResponse:
    _check_idle()
    model = str(body.get("model") or "")
    msgs = [m for m in body.get("messages") or [] if m.get("role") in ("user", "assistant") and m.get("content")]
    if not model or not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(400, "Thiếu model hoặc câu hỏi")
    use_docs, use_web = bool(body.get("use_docs", True)), bool(body.get("use_web"))

    async def gen() -> AsyncIterator[str]:
        def ev(**k: Any) -> str:
            return json.dumps(k, ensure_ascii=False) + "\n"

        try:
            yield ev(type="status", text="Đang nạp model…")
            ctx = await _prepare_model(model)
            history, question = msgs[-7:-1], msgs[-1]["content"]
            prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
            used = sum(len(m["content"]) for m in history) + len(question)
            budget = max(1500, min(40000, int((ctx - 1500) * 2.2) - used))
            passages: list[dict[str, Any]] = []
            if use_docs:
                ix = index()
                if not ix.chunks:
                    raise RuntimeError("Chưa có chỉ mục tài liệu. Hãy bấm \"Cập nhật chỉ mục\" trước.")
                yield ev(type="status", text="Đang tìm tài liệu liên quan…")
                await _prepare_embed(ix.model)
                found = await rag.retrieve(ix, f"{prev}\n{question}".strip(), budget * 6 // 10 if use_web else budget)  # kèm câu trước để hiểu câu hỏi nối tiếp
                passages += [{"kind": "doc", "doc": c["doc"], "i": c["i"], "loc": c.get("loc"), "text": c["text"]} for c in found]
            if use_web:
                yield ev(type="status", text="Đang tìm kiếm trên internet…")
                q = question if len(question) > 25 or not prev else f"{prev} {question}"
                passages += await _web_passages(q, budget - sum(len(x["text"]) for x in passages))
            for n, x in enumerate(passages, 1):
                x["n"] = n
            yield ev(type="sources", ctx=ctx, sources=[{k: v for k, v in x.items() if k != "text" or x["kind"] == "web"} for x in passages])

            content, system = question, SYSTEM_PLAIN
            if use_docs or use_web:
                blocks = []
                for x in passages:
                    if x["kind"] == "doc":
                        where = rag.loc_text(x["loc"])
                        blocks.append(f"[{x['n']}] Tài liệu: {x['doc']}{' — ' + where if where else ''}\n{x['text']}")
                    else:
                        blocks.append(f"[{x['n']}] Trang web: {x['title']} ({x['url']})\n{x['text']}")
                content = "NGUỒN THAM KHẢO:\n" + ("\n\n".join(blocks) or "(không tìm thấy nguồn nào)") + f"\n\nCÂU HỎI: {question}"
                system = SYSTEM + (f" Hôm nay là ngày {time.strftime('%d/%m/%Y')}." if use_web else "")
            payload = {"model": model, "stream": True, "temperature": 0.2, "max_tokens": -1, "stream_options": {"include_usage": True},
                       "messages": [{"role": "system", "content": system + FORMAT}, *history, {"role": "user", "content": content}]}
            t0, tokens, thinking = time.perf_counter(), 0, False
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
                            yield ev(type="text", text=d["content"])
                        elif (d.get("reasoning_content") or d.get("reasoning")) and not thinking:
                            thinking = True
                            yield ev(type="status", text="Model đang suy nghĩ…")
            total = time.perf_counter() - t0
            yield ev(type="done", seconds=round(total, 1), tokens=tokens, tps=round(tokens / total, 1) if total > 0 else 0)
        except Exception as e:  # noqa: BLE001
            yield ev(type="error", text=str(e) or type(e).__name__)

    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
