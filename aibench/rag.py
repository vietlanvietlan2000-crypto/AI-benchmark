"""Tra cứu tài liệu (RAG) cho chatbot.

Tài liệu đặt trong thư mục Tai_lieu/ (chia thư mục con tùy ý). Cách làm:
  1. Lập chỉ mục: đọc từng file, cắt thành đoạn, nhờ model embedding của LM Studio đổi mỗi đoạn thành vector.
  2. Khi hỏi: tìm các đoạn gần câu hỏi nhất để biết tài liệu nào liên quan (bước "định hướng"),
     rồi đưa cho AI đọc cả tài liệu đó nếu vừa ngữ cảnh, hoặc các đoạn liên quan nếu tài liệu quá dài.

Chỉ mục lưu ở rag_index/ và chỉ tính lại những file đã đổi.
"""
from __future__ import annotations

import asyncio
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from .core import LM_URL, ROOT

DOCS = ROOT / "Tai_lieu"
INDEX = ROOT / "rag_index"
EXTS = {".txt", ".md", ".csv", ".pdf", ".docx", ".xlsx"}
CHUNK = 1200  # ký tự mỗi đoạn


# ----- đọc file ---------------------------------------------------------------------------------
def read_doc(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
        lines = ["".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)) for p in paras]
        text = "\n".join(x for x in lines if x.strip())
        return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"[Sheet: {ws.title}]")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1258"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("utf-8", "replace")


def split(text: str) -> list[str]:
    """Cắt theo đoạn văn, gom lại cho gần CHUNK ký tự. Nối các đoạn lại là ra nguyên văn bản."""
    parts: list[str] = []
    for para in re.split(r"\n\s*\n|\n(?=#)", text.replace("\r", "")):
        para = para.strip()
        while len(para) > CHUNK * 1.5:  # đoạn quá dài (bảng, PDF không xuống dòng) thì cắt ở khoảng trắng
            cut = para.rfind(" ", 0, CHUNK)
            cut = cut if cut > CHUNK // 2 else CHUNK
            parts.append(para[:cut])
            para = para[cut:].strip()
        if para:
            parts.append(para)
    out: list[str] = []
    for p in parts:
        if out and len(out[-1]) + len(p) < CHUNK:
            out[-1] += "\n\n" + p
        else:
            out.append(p)
    return out


def list_files() -> list[Path]:
    if not DOCS.is_dir():
        return []
    return sorted(p for p in DOCS.rglob("*") if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith("~$"))


def rel(p: Path) -> str:
    return p.relative_to(DOCS).as_posix()


# ----- embedding --------------------------------------------------------------------------------
def _prefix(model: str, query: bool) -> str:
    """Một số model embedding cần câu dẫn riêng cho câu hỏi và cho tài liệu."""
    m = model.lower()
    if "nomic" in m:
        return "search_query: " if query else "search_document: "
    if "qwen3-embedding" in m and query:
        return "Instruct: Given a question, retrieve passages from internal documents that answer it\nQuery: "
    if "e5" in m:
        return "query: " if query else "passage: "
    return ""


async def embed(model: str, texts: list[str], query: bool = False) -> np.ndarray:
    pre = _prefix(model, query)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as http:
        for attempt in (1, 2):  # lần đầu LM Studio phải nạp model embedding, đôi khi lỗi thoáng qua
            r = await http.post(f"{LM_URL}/v1/embeddings", json={"model": model, "input": [pre + t for t in texts]})
            if r.status_code == 200 or attempt == 2:
                break
            await asyncio.sleep(3)
    if r.status_code != 200:
        hint = " Có thể VRAM đã đầy: hãy gỡ bớt model đang nạp hoặc chọn model embedding nhỏ hơn." if "load" in r.text.lower() else ""
        raise RuntimeError(f"LM Studio trả lỗi {r.status_code} khi tạo embedding: {r.text[:300]}{hint}")
    v = np.array([d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])], dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


# ----- chỉ mục ----------------------------------------------------------------------------------
class Index:
    def __init__(self) -> None:
        self.model = ""
        self.files: dict[str, dict[str, Any]] = {}  # rel -> {mtime, size, n, note}
        self.chunks: list[dict[str, Any]] = []  # {doc, i, text}
        self.vecs = np.zeros((0, 1), dtype=np.float32)

    @classmethod
    def load(cls) -> "Index":
        ix = cls()
        try:
            meta = json.loads((INDEX / "index.json").read_text(encoding="utf-8"))
            vecs = np.load(INDEX / "vectors.npy")
            if len(meta["chunks"]) == len(vecs):
                ix.model, ix.files, ix.chunks, ix.vecs = meta["model"], meta["files"], meta["chunks"], vecs
        except (OSError, ValueError, KeyError):
            pass
        return ix

    def save(self) -> None:
        INDEX.mkdir(exist_ok=True)
        np.save(INDEX / "vectors.npy", self.vecs)
        (INDEX / "index.json").write_text(
            json.dumps({"model": self.model, "files": self.files, "chunks": self.chunks}, ensure_ascii=False), encoding="utf-8")

    def stale(self) -> list[str]:
        """Các file mới hoặc đã đổi so với chỉ mục."""
        out = []
        for p in list_files():
            f, s = self.files.get(rel(p)), p.stat()
            if not f or f["mtime"] != s.st_mtime or f["size"] != s.st_size:
                out.append(rel(p))
        return out

    def doc_text(self, doc: str) -> str:
        return "\n\n".join(c["text"] for c in self.chunks if c["doc"] == doc)


async def build(model: str, progress: dict[str, Any]) -> Index:
    """Lập lại chỉ mục; file không đổi (và cùng model embedding) thì dùng lại vector cũ."""
    old = Index.load()
    reuse = old.model == model
    new = Index()
    new.model = model
    vecs: list[np.ndarray] = []
    files = list_files()
    progress.update(total=len(files), done=0, file="")
    for p in files:
        r, s = rel(p), p.stat()
        progress["file"] = r
        f = old.files.get(r)
        if reuse and f and f["mtime"] == s.st_mtime and f["size"] == s.st_size:
            idx = [k for k, c in enumerate(old.chunks) if c["doc"] == r]
            new.chunks += [old.chunks[k] for k in idx]
            if idx:
                vecs.append(old.vecs[idx])
            new.files[r] = f
        else:
            note = ""
            try:
                parts = split(read_doc(p))
                if not parts:
                    note = "empty"  # không có chữ (thường là PDF scan)
            except Exception as e:  # noqa: BLE001
                parts, note = [], f"error: {e}"
            head = r.rsplit(".", 1)[0].replace("/", " > ")  # đường dẫn thư mục giúp tìm đúng tài liệu
            for b in range(0, len(parts), 16):
                vecs.append(await embed(model, [f"{head}\n{t}" for t in parts[b:b + 16]]))
            new.chunks += [{"doc": r, "i": i, "text": t} for i, t in enumerate(parts)]
            new.files[r] = {"mtime": s.st_mtime, "size": s.st_size, "n": len(parts), "note": note}
        progress["done"] += 1
    new.vecs = np.concatenate(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
    new.save()
    return new


# ----- tìm và dựng ngữ cảnh ---------------------------------------------------------------------
def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 1}


async def retrieve(ix: Index, query: str, budget: int, top_docs: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """Trả (ngữ cảnh cho AI đọc, danh sách nguồn). `budget` là số ký tự tối đa của ngữ cảnh."""
    if not ix.chunks:
        return "", []
    q = (await embed(ix.model, [query], query=True))[0]
    score = ix.vecs @ q
    qw = _words(query)
    if qw:  # cộng điểm trùng từ khóa: bù cho model embedding yếu tiếng Việt, tên riêng, mã số
        kw = np.array([len(qw & _words(c["doc"] + " " + c["text"])) / len(qw) for c in ix.chunks], dtype=np.float32)
        score = score + 0.3 * kw
    best = np.argsort(-score)[:10]
    docs: dict[str, float] = {}
    for k in best:  # điểm tài liệu = đoạn tốt nhất + chút thưởng cho mỗi đoạn trúng thêm
        d = ix.chunks[k]["doc"]
        docs[d] = docs[d] + 0.05 if d in docs else float(score[k])
    ranked = sorted(docs, key=lambda d: -docs[d])[:top_docs]
    ranked = [d for d in ranked if docs[d] >= docs[ranked[0]] - 0.25]  # bỏ tài liệu kém xa tài liệu đầu

    out, sources, left = [], [], budget
    for n, d in enumerate(ranked):
        full = ix.doc_text(d)
        share = left if n == len(ranked) - 1 else int(left * 0.6)
        if len(full) <= share:
            body, mode = full, "full"
        else:  # tài liệu dài: lấy các đoạn trúng kèm đoạn liền trước/sau, theo đúng thứ tự trong tài liệu
            mine = [c for c in ix.chunks if c["doc"] == d]
            hits = [ix.chunks[k]["i"] for k in best if ix.chunks[k]["doc"] == d]
            want: list[int] = []
            for i in hits:
                for j in (i, i - 1, i + 1):
                    if 0 <= j < len(mine) and j not in want:
                        want.append(j)
            keep, used = [], 0
            for j in want:
                if used + len(mine[j]["text"]) > share:
                    break
                keep.append(j)
                used += len(mine[j]["text"])
            if not keep:
                continue
            keep.sort()
            body = "\n\n".join(("[...]\n" if a and keep[a - 1] != j - 1 else "") + mine[j]["text"] for a, j in enumerate(keep))
            mode = "excerpt"
        out.append(f"=== TÀI LIỆU: {d} ===\n{body}")
        left -= len(body)
        sources.append({"doc": d, "mode": mode, "score": round(docs[d], 3)})
        if left < 500:
            break
    return "\n\n".join(out), sources
