"""Tra cứu tài liệu (RAG) cho chatbot.

Tài liệu đặt trong thư mục Tai_lieu/ (chia thư mục con tùy ý). Cách làm:
  1. Lập chỉ mục: đọc từng file, cắt thành đoạn (ghi lại đoạn nằm ở trang nào / mục nào / sheet nào),
     nhờ model embedding của LM Studio đổi mỗi đoạn thành vector.
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
CHUNK = 900  # ký tự mỗi đoạn
VERSION = 2  # đổi cách cắt đoạn thì tăng số này để chỉ mục cũ bị bỏ

Loc = list | None  # ["page", 3] | ["sheet", "Bảng giá"] | ["sec", "2. Thủ tục xin nghỉ"] | None


# ----- đọc file ---------------------------------------------------------------------------------
def _unxml(s: str) -> str:
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def read_doc(path: Path) -> list[tuple[Loc, str]]:
    """Các phần của tài liệu kèm vị trí: mỗi trang PDF, mỗi sheet Excel, mỗi mục (theo tiêu đề) của Word/Markdown."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        return [(["page", n], pg.extract_text() or "") for n, pg in enumerate(PdfReader(str(path)).pages, 1)]
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        out: list[tuple[Loc, str]] = [(None, "")]
        for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
            text = _unxml("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))).strip()
            if not text:
                continue
            # Kiểu tiêu đề: "Heading1" (Word tiếng Anh), "u1" (Word tiếng Việt), "Title"
            if re.search(r'<w:pStyle w:val="(Heading\d|u\d|Title)"', p) and len(text) < 200:
                out.append((["sec", text], text))
            else:
                out[-1] = (out[-1][0], (out[-1][1] + "\n" + text).strip())
        return out
    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            rows = [" | ".join(str(c) for c in row if c is not None) for row in ws.iter_rows(values_only=True)]
            out.append((["sheet", ws.title], "\n".join(r for r in rows if r)))
        return out
    raw = path.read_bytes()
    text = raw.decode("utf-8", "replace")
    for enc in ("utf-8-sig", "utf-16", "cp1258"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeError:
            continue
    out = [(None, "")]
    for line in text.replace("\r", "").split("\n"):  # tiêu đề Markdown (# ...) mở một mục mới
        m = re.match(r"#{1,6}\s+(.+)", line)
        if m:
            out.append((["sec", m.group(1).strip()], line))
        else:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + line)
    return out


def split(text: str) -> list[str]:
    """Cắt theo đoạn văn, gom lại cho gần CHUNK ký tự."""
    parts: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        while len(para) > CHUNK * 1.5:  # đoạn quá dài (bảng, PDF không xuống dòng) thì cắt ở chỗ xuống dòng / khoảng trắng
            cut = max(para.rfind("\n", 0, CHUNK), para.rfind(" ", 0, CHUNK))
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


def doc_chunks(path: Path) -> list[dict[str, Any]]:
    out = []
    for loc, text in read_doc(path):
        out += [{"loc": loc, "text": t} for t in split(text)]
    return out


def loc_text(loc: Loc) -> str:
    """Vị trí viết cho AI đọc (tiếng Việt)."""
    if not loc:
        return ""
    return {"page": "trang", "sheet": "sheet", "sec": "mục"}[loc[0]] + f" {loc[1]}"


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
        self.chunks: list[dict[str, Any]] = []  # {doc, i, loc, text}
        self.vecs = np.zeros((0, 1), dtype=np.float32)

    @classmethod
    def load(cls) -> "Index":
        ix = cls()
        try:
            meta = json.loads((INDEX / "index.json").read_text(encoding="utf-8"))
            vecs = np.load(INDEX / "vectors.npy")
            if meta.get("version") == VERSION and len(meta["chunks"]) == len(vecs):
                ix.model, ix.files, ix.chunks, ix.vecs = meta["model"], meta["files"], meta["chunks"], vecs
        except (OSError, ValueError, KeyError):
            pass
        return ix

    def save(self) -> None:
        INDEX.mkdir(exist_ok=True)
        np.save(INDEX / "vectors.npy", self.vecs)
        (INDEX / "index.json").write_text(json.dumps(
            {"version": VERSION, "model": self.model, "files": self.files, "chunks": self.chunks}, ensure_ascii=False), encoding="utf-8")

    def stale(self) -> list[str]:
        """Các file mới hoặc đã đổi so với chỉ mục."""
        out = []
        for p in list_files():
            f, s = self.files.get(rel(p)), p.stat()
            if not f or f["mtime"] != s.st_mtime or f["size"] != s.st_size:
                out.append(rel(p))
        return out

    def of(self, doc: str) -> list[dict[str, Any]]:
        return [c for c in self.chunks if c["doc"] == doc]


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
                parts = await asyncio.to_thread(doc_chunks, p)
                if not parts:
                    note = "empty"  # không có chữ (thường là PDF scan)
            except Exception as e:  # noqa: BLE001
                parts, note = [], f"error: {e}"
            head = r.rsplit(".", 1)[0].replace("/", " > ")  # đường dẫn thư mục và tên mục giúp tìm đúng tài liệu
            texts = [f"{head}{' > ' + str(c['loc'][1]) if c['loc'] and c['loc'][0] == 'sec' else ''}\n{c['text']}" for c in parts]
            for b in range(0, len(texts), 16):
                vecs.append(await embed(model, texts[b:b + 16]))
            new.chunks += [{"doc": r, "i": i, **c} for i, c in enumerate(parts)]
            new.files[r] = {"mtime": s.st_mtime, "size": s.st_size, "n": len(parts), "note": note}
        progress["done"] += 1
    new.vecs = np.concatenate(vecs) if vecs else np.zeros((0, 1), dtype=np.float32)
    new.save()
    return new


# ----- tìm và chọn đoạn cho AI đọc --------------------------------------------------------------
def words(s: str) -> set[str]:
    return {w for w in re.findall(r"\w+", s.lower()) if len(w) > 1}


async def retrieve(ix: Index, query: str, budget: int, top_docs: int = 3) -> list[dict[str, Any]]:
    """Các đoạn cho AI đọc (theo thứ tự tài liệu, rồi thứ tự trong tài liệu). `budget` là tổng số ký tự tối đa.
    Tài liệu vừa ngữ cảnh thì lấy trọn; tài liệu dài thì lấy đoạn trúng kèm đoạn liền trước/sau."""
    if not ix.chunks:
        return []
    q = (await embed(ix.model, [query], query=True))[0]
    score = ix.vecs @ q
    qw = words(query)
    if qw:  # cộng điểm trùng từ khóa: bù cho model embedding yếu tiếng Việt, tên riêng, mã số
        kw = np.array([len(qw & words(c["doc"] + " " + c["text"])) / len(qw) for c in ix.chunks], dtype=np.float32)
        score = score + 0.3 * kw
    best = np.argsort(-score)[:10]
    docs: dict[str, float] = {}
    for k in best:  # điểm tài liệu = đoạn tốt nhất + chút thưởng cho mỗi đoạn trúng thêm
        d = ix.chunks[k]["doc"]
        docs[d] = docs[d] + 0.05 if d in docs else float(score[k])
    ranked = sorted(docs, key=lambda d: -docs[d])[:top_docs]
    ranked = [d for d in ranked if docs[d] >= docs[ranked[0]] - 0.25]  # bỏ tài liệu kém xa tài liệu đầu

    out: list[dict[str, Any]] = []
    left = budget
    for n, d in enumerate(ranked):
        mine = ix.of(d)
        share = left if n == len(ranked) - 1 else int(left * 0.6)
        if sum(len(c["text"]) for c in mine) <= share:
            keep = list(range(len(mine)))
        else:
            want: list[int] = []
            for i in (ix.chunks[k]["i"] for k in best if ix.chunks[k]["doc"] == d):
                for j in (i, i - 1, i + 1):
                    if 0 <= j < len(mine) and j not in want:
                        want.append(j)
            keep, used = [], 0
            for j in want:
                if used + len(mine[j]["text"]) > share:
                    break
                keep.append(j)
                used += len(mine[j]["text"])
            keep.sort()
        out += [mine[j] for j in keep]
        left -= sum(len(mine[j]["text"]) for j in keep)
        if left < 500:
            break
    return out
