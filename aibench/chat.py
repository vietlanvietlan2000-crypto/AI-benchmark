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

from . import pages, rag, websearch
from .core import LM_URL
from .lmstudio import LMStudio, _lms

router = APIRouter(prefix="/api/chat")
busy: Callable[[], Any] = lambda: None  # server.py gán: trả job benchmark đang chạy (nếu có)
indexing: dict[str, Any] = {"running": False, "stage": "", "done": 0, "total": 0, "file": "", "error": None, "cancel": False}
_index: rag.Index | None = None

CTX_WANT = 16384  # ngữ cảnh khi chatbot tự nạp model (đủ để đọc trọn tài liệu vài trang)
EMBED_CTX = 4096  # ngữ cảnh của model embedding: mỗi đoạn tài liệu chỉ dài khoảng 1.200 ký tự
OCR_CTX = 16384  # ngữ cảnh của model OCR: ảnh 1 trang ~2.200 token + chữ đọc ra, nhân số trang OCR cùng lúc
SYSTEM = (
    "Bạn là trợ lý tra cứu thân thiện, trả lời như một đồng nghiệp am hiểu tài liệu: tự nhiên, rõ ràng, đi thẳng vào ý chính. "
    "Thông tin thực tế (quy định, số liệu, thông số, quy trình…) chỉ lấy từ các NGUỒN THAM KHẢO được đánh số trong tin nhắn; "
    "sau mỗi ý lấy từ nguồn, ghi số nguồn trong ngoặc vuông, ví dụ [2] hoặc [1][3]. "
    "Nếu các nguồn không có thông tin cần hỏi, nói thẳng là chưa tìm thấy trong tài liệu và gợi ý cách hỏi khác, không tự bịa. "
    "Câu chào hỏi, cảm ơn hay trao đổi xã giao thì đáp lại tự nhiên, không cần nhắc tới nguồn. "
    "Trả lời bằng ngôn ngữ của người dùng."
)
SYSTEM_PLAIN = "Bạn là trợ lý AI thân thiện, hữu ích. Trò chuyện tự nhiên, trả lời rõ ràng, bằng ngôn ngữ của người dùng."
SYSTEM_CHAT = (
    "Bạn là trợ lý AI thân thiện của doanh nghiệp, có thể tra cứu kho tài liệu nội bộ{docs}. "
    "Tin nhắn này không cần tra tài liệu: hãy trò chuyện tự nhiên, ngắn gọn, ấm áp, bằng ngôn ngữ của người dùng. "
    "Khi được hỏi bạn làm được gì: giới thiệu bạn tra cứu tài liệu nội bộ và trích dẫn trang/mục, lập bảng, xuất Excel, vẽ biểu đồ"
    "{web}. Đừng tự bịa nội dung tài liệu; nếu người dùng cần thông tin trong tài liệu, mời họ hỏi cụ thể."
)
ROUTER = (
    "Bạn là bộ phân loại tin nhắn cho một trợ lý tra cứu tài liệu nội bộ{docs}. Nguồn tra cứu đang bật: {sources}.\n"
    "Đọc đoạn hội thoại và TIN NHẮN CUỐI của người dùng, trả về DUY NHẤT một dòng JSON:\n"
    '{{"action": "chat" hoặc "search", "query": "..."}}\n'
    '- "chat": chào hỏi, cảm ơn, tạm biệt, khen chê, hỏi bạn là ai / làm được gì, tán gẫu; yêu cầu viết lại, dịch, tóm tắt, '
    "rút gọn, đổi định dạng, lập bảng, vẽ biểu đồ từ nội dung ĐÃ CÓ trong hội thoại.\n"
    '- "search": cần thông tin thực tế (quy định, chính sách, số liệu, thông số kỹ thuật, quy trình, sản phẩm, giá…) '
    'hoặc hỏi về nội dung tài liệu. Khi phân vân, chọn "search".\n'
    '- "query" (khi search): viết lại tin nhắn cuối thành câu tìm kiếm đầy đủ, tự hiểu được mà không cần hội thoại '
    "(thay \"nó\", \"cái đó\", \"còn … thì sao\" bằng đối tượng cụ thể), giữ nguyên ngôn ngữ và thuật ngữ, mã hiệu.\n"
    "Không giải thích, không thêm chữ nào ngoài JSON."
)
# Câu xã giao rõ ràng: không cần hỏi model phân loại
SMALLTALK = re.compile(
    r"^\s*(hi+|hello+|hey+|helo|alo+|chào|xin chào|chào (bạn|em|anh|chị|bot|ad)|hế lô|hi there|good (morning|afternoon|evening)|"
    r"cảm ơn|cám ơn|cảm ơn (bạn|em|nhiều|nha|nhé)|thanks?( you)?|thank you( so much)?|tks|ok+|oke+|okay|được rồi|tốt lắm|tuyệt|"
    r"bye|tạm biệt|bái bai|hẹn gặp lại|bạn (là ai|tên gì)|you are who|who are you)"
    r"[\s!.?,~:)(^❤️👍😊🙂]*((bạn|nhé|nha|nhiều|ạ|à|ơi|bot|em|anh|chị|so much|a lot)[\s!.?,~:)(^❤️👍😊🙂]*){0,3}$", re.I)
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
    if indexing["running"]:
        raise HTTPException(409, "Đang lập chỉ mục tài liệu, hãy đợi xong hoặc bấm Dừng")


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
                          "chunks": f.get("n", 0), "note": f.get("note", ""), "ocr_pages": f.get("ocr_pages", 0),
                          "ocr_missing": f.get("ocr_missing", 0)})
        return {"docs_dir": str(rag.DOCS), "exts": sorted(rag.EXTS), "files": files, "embed_model": ix.model,
                "chunks": len(ix.chunks), "indexing": indexing}

    return await asyncio.to_thread(work)


@router.post("/index")
async def build_index(body: dict[str, Any]) -> dict[str, Any]:
    if indexing["running"]:
        return {"ok": True}
    _check_idle()
    model, ocr_model = str(body.get("embed_model") or ""), str(body.get("ocr_model") or "")
    if not model:
        raise HTTPException(400, "Chưa chọn model embedding")
    indexing.update(running=True, stage="scan", done=0, total=0, file="", error=None, cancel=False)

    async def prepare_ocr(m: str) -> None:
        """Gỡ mọi model khác để model OCR có đủ VRAM, rồi nạp model OCR."""
        lm = LMStudio(LM_URL)
        try:
            models = await lm.list_models()
        finally:
            await lm.close()
        info = next((x for x in models if x["id"] == m), None)
        if info is None:
            raise RuntimeError(f"LM Studio không có model {m}")
        for x in models:
            if x.get("state") == "loaded" and x["id"] != m:
                await _lms("unload", x["id"], timeout=120)
        if info.get("state") != "loaded":
            await _lms("load", m, "-y", "-c", str(min(OCR_CTX, info.get("max_context_length") or OCR_CTX)))

    async def prepare_embed(m: str) -> None:
        if ocr_model and ocr_model != m:  # OCR xong thì gỡ model OCR để nhường VRAM
            try:
                await _lms("unload", ocr_model, timeout=120)
            except RuntimeError:
                pass  # model OCR không được nạp (không có trang nào cần OCR)
        await _prepare_embed(m)

    async def work() -> None:
        global _index
        try:
            _index = await rag.build(model, ocr_model, indexing, prepare_ocr, prepare_embed)
        except rag.Cancelled:
            indexing["error"] = "Đã dừng. Phần đã làm (trang đã OCR, tài liệu đã lưu) được giữ lại, bấm Cập nhật chỉ mục để làm tiếp."
        except Exception as e:  # noqa: BLE001
            indexing["error"] = str(e) or type(e).__name__
        finally:
            if indexing["error"]:
                _index = None  # đọc lại từ cơ sở dữ liệu những tài liệu đã lưu được
            indexing.update(running=False, stage="", cancel=False)

    asyncio.get_running_loop().create_task(work())
    return {"ok": True}


@router.post("/index/cancel")
async def cancel_index() -> dict[str, Any]:
    if indexing["running"]:
        indexing["cancel"] = True
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
    return {"path": path, "chunks": [{"i": c["i"], "loc": c.get("loc"), "text": c["text"], "ocr": c.get("ocr", False)} for c in chunks]}


@router.get("/page")
async def get_page(path: str, page: int = 1) -> Response:
    """Ảnh 1 trang của tài liệu gốc (PDF / file ảnh)."""
    try:
        p = pages.resolve(path)
        if not pages.kind(p) or not 1 <= page <= pages.page_count(p):
            raise FileNotFoundError(path)
        jpg = await asyncio.to_thread(pages.render, p, page)
    except FileNotFoundError:
        raise HTTPException(404, "Không có trang này") from None
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "max-age=300"})


@router.post("/marks")
async def get_marks(body: dict[str, Any]) -> dict[str, Any]:
    """Khung tô vàng trên 1 trang: các dòng khớp với câu trả lời. body: {path, page, answer}."""
    try:
        p = pages.resolve(str(body.get("path", "")))
        if not pages.kind(p):
            raise FileNotFoundError
        return await asyncio.to_thread(pages.marks, p, int(body.get("page") or 1), str(body.get("answer") or ""))
    except FileNotFoundError:
        raise HTTPException(404, "Tài liệu này không xem được dạng trang") from None


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


def _catalog(limit: int = 30) -> str:
    """Tên ngắn các tài liệu đang có, để model biết kho tài liệu nói về gì."""
    names = sorted(index().files)
    if not names:
        return ""
    short = [re.sub(r"\.\w+$", "", n.rsplit("/", 1)[-1]) for n in names[:limit]]  # chỉ tên file, không đường dẫn
    more = f" và {len(names) - limit} tài liệu khác" if len(names) > limit else ""
    return " (gồm: " + "; ".join(short) + more + ")"


async def _route(model: str, history: list[dict[str, Any]], question: str, use_docs: bool, use_web: bool) -> tuple[str, str]:
    """Phân loại tin nhắn: ("chat", "") nếu không cần tra cứu, ("search", câu tìm kiếm đã viết lại) nếu cần.
    Câu xã giao rõ ràng nhận ra ngay; còn lại hỏi nhanh model đang dùng. Lỗi hay trả lời lạ thì coi như cần tra cứu."""
    if SMALLTALK.match(question):
        return "chat", ""
    sources = " và ".join(x for x, on in (("tài liệu nội bộ", use_docs), ("internet", use_web)) if on)
    convo = "\n".join(f"{'Người dùng' if m['role'] == 'user' else 'Trợ lý'}: {m['content'][:600]}" for m in history[-4:])
    payload = {"model": model, "temperature": 0, "max_tokens": 600, "reasoning_effort": "none", "stream": False,
               "messages": [{"role": "system", "content": ROUTER.format(docs=_catalog() if use_docs else "", sources=sources)},
                            {"role": "user", "content": (f"HỘI THOẠI TRƯỚC:\n{convo}\n\n" if convo else "") + f"TIN NHẮN CUỐI: {question}"}]}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as http:
            r = await http.post(f"{LM_URL}/v1/chat/completions", json=payload)
        text = r.json()["choices"][0]["message"].get("content") or ""
        m = re.search(r"\{.*\}", text, re.S)
        obj = json.loads(m.group()) if m else {}
    except Exception:  # noqa: BLE001
        return "search", ""
    if str(obj.get("action", "")).lower() == "chat":
        return "chat", ""
    return "search", str(obj.get("query") or "").strip()


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
            action, query = "plain", ""  # plain: người dùng tắt cả tài liệu lẫn internet
            if use_docs or use_web:
                yield ev(type="status", text="Đang hiểu câu hỏi…")
                action, query = await _route(model, history, question, use_docs, use_web)
                yield ev(type="route", mode=action, query=query)
            chatting = action == "chat"
            docs_on, web_on = use_docs and not chatting, use_web and not chatting
            if docs_on:
                ix = index()
                if not ix.chunks:
                    raise RuntimeError("Chưa có chỉ mục tài liệu. Hãy bấm \"Cập nhật chỉ mục\" trước.")
                yield ev(type="status", text="Đang tìm tài liệu liên quan…")
                await _prepare_embed(ix.model)
                # câu tìm kiếm đã được viết lại cho đủ ý; không có thì ghép câu trước để hiểu câu hỏi nối tiếp
                q = f"{query}\n{question}" if query else f"{prev}\n{question}".strip()
                found = await rag.retrieve(ix, q, budget * 6 // 10 if web_on else budget)
                passages += [{"kind": "doc", "doc": c["doc"], "i": c["i"], "loc": c.get("loc"), "text": c["text"]} for c in found]
            if web_on:
                yield ev(type="status", text="Đang tìm kiếm trên internet…")
                q = query or (question if len(question) > 25 or not prev else f"{prev} {question}")
                passages += await _web_passages(q, budget - sum(len(x["text"]) for x in passages))
            for n, x in enumerate(passages, 1):
                x["n"] = n
            yield ev(type="sources", ctx=ctx, sources=[{k: v for k, v in x.items() if k != "text" or x["kind"] == "web"} for x in passages])

            content, system = question, SYSTEM_PLAIN
            if chatting:  # trò chuyện: không đưa nguồn, để model đáp tự nhiên
                system = SYSTEM_CHAT.format(docs=_catalog(12) if use_docs else "", web=", tìm kiếm trên internet" if use_web else "")
            elif docs_on or web_on:
                blocks = []
                for x in passages:
                    if x["kind"] == "doc":
                        where = rag.loc_text(x["loc"])
                        blocks.append(f"[{x['n']}] Tài liệu: {x['doc']}{' — ' + where if where else ''}\n{x['text']}")
                    else:
                        blocks.append(f"[{x['n']}] Trang web: {x['title']} ({x['url']})\n{x['text']}")
                content = "NGUỒN THAM KHẢO:\n" + ("\n\n".join(blocks) or "(không tìm thấy nguồn nào)") + f"\n\nCÂU HỎI: {question}"
                system = SYSTEM + (f" Hôm nay là ngày {time.strftime('%d/%m/%Y')}." if use_web else "")
            payload = {"model": model, "stream": True, "temperature": 0.6 if chatting else 0.2, "max_tokens": -1, "stream_options": {"include_usage": True},
                       "messages": [{"role": "system", "content": system + FORMAT}, *history, {"role": "user", "content": content}]}
            if chatting:  # trò chuyện không cần model suy nghĩ lâu: trả lời ngay
                payload["reasoning_effort"] = "none"
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
