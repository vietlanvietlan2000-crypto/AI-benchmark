"""Trình cài đặt từng bước. Chạy: python -m aibench setup   (install.ps1 tự gọi sau khi tải mã nguồn)

  [1/5] Kiểm tra phần cứng      GPU, VRAM, RAM, ổ đĩa
  [2/5] Kiểm tra phần mềm       driver NVIDIA, LM Studio, lệnh lms, server LM Studio; thiếu thì hỏi rồi cài
  [3/5] Nơi lưu model           đổi thư mục model của LM Studio khi ổ đĩa sắp đầy
  [4/5] Gợi ý và tải model      theo VRAM/RAM của máy (danh sách ở catalog.json)
  [5/5] Hoàn tất                lối tắt ngoài Desktop, mở ứng dụng

Chỉ dùng thư viện chuẩn để chạy được ngay cả khi các gói khác chưa cài xong.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from .engine import ram_total_mb
from .gpu import gpu_info

ROOT = Path(__file__).resolve().parent.parent
LMS_HOME = Path.home() / ".lmstudio"
SETTINGS = LMS_HOME / "settings.json"
WINGET_ID = "ElementLabs.LMStudio"
STEPS = 5
EN = False
YES = False


def L(vi: str, en: str) -> str:
    return en if EN else vi


def color(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def step(n: int, title: str) -> None:
    done = "█" * n + "░" * (STEPS - n)
    print("\n" + color("96;1", f"[{n}/{STEPS}] {done}  {title}"))


def ok(s: str) -> None:
    print("  " + color("92", "✓") + " " + s)


def warn(s: str) -> None:
    print("  " + color("93", "!") + " " + s)


def bad(s: str) -> None:
    print("  " + color("91", "✗") + " " + s)


def info(s: str) -> None:
    print("    " + s)


def ask(question: str, default: str = "") -> str:
    """Hỏi người dùng; Enter (hoặc chế độ --yes, hoặc không có bàn phím) lấy giá trị mặc định."""
    if YES:
        print(f"  ? {question} → {default or '-'}")
        return default
    try:
        return input(color("97;1", f"  ? {question} ")).strip() or default
    except EOFError:
        print()
        return default


def yes(question: str, default: bool = True) -> bool:
    a = ask(question + (" [Y/n]" if default else " [y/N]"), "y" if default else "n").lower()
    return a[:1] in ("y", "c")  # y = yes, c = có


def run_live(cmd: list[str]) -> int:
    """Chạy lệnh và để nó in thẳng tiến độ ra màn hình."""
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        bad(str(e))
        return 1


def gb(mb: float) -> str:
    return f"{mb / 1024:.1f}".replace(".", "," if not EN else ".")


# ----- phần cứng -------------------------------------------------------------------------------
def other_gpus() -> list[str]:
    """Tên các card đồ họa Windows thấy (dùng khi không có nvidia-smi)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-CimInstance Win32_VideoController).Name"],
                             capture_output=True, text=True, timeout=20).stdout
        return [x.strip() for x in out.splitlines() if x.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def drives() -> list[tuple[str, float, float]]:
    """[(ổ, GB trống, GB tổng)] của các ổ đĩa cố định."""
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) != 3:  # 3 = ổ cố định
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        out.append((root, u.free / 2**30, u.total / 2**30))
    return out


def free_gb(path: Path) -> float:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 2**30
    except OSError:
        return 0.0


def check_hardware() -> dict[str, Any]:
    step(1, L("Kiểm tra phần cứng", "Checking hardware"))
    hw: dict[str, Any] = {"gpu": gpu_info(), "ram_mb": ram_total_mb() or 0}
    g = hw["gpu"]
    if g.get("name"):
        ok(f"GPU: {g['name']} · {gb(g['vram_total_mb'])} GB VRAM · driver {g['driver']}")
    else:
        names = other_gpus()
        warn(L("Không thấy GPU NVIDIA (không có lệnh nvidia-smi).", "No NVIDIA GPU found (nvidia-smi is missing)."))
        if names:
            info(L("Card đồ họa trong máy: ", "Graphics adapters: ") + ", ".join(names))
        info(L("AI Bench vẫn chạy được nhưng không đo được VRAM, nhiệt độ và điện năng; model sẽ chạy bằng CPU hoặc GPU khác nên chậm.",
               "AI Bench still runs, but VRAM, temperature and power cannot be measured; models will run on CPU or another GPU and be slow."))
        info(L("Nếu máy có card NVIDIA: cài driver tại https://www.nvidia.com/drivers rồi chạy lại.",
               "If this PC has an NVIDIA card: install the driver from https://www.nvidia.com/drivers and run setup again."))
    ok(f"RAM: {gb(hw['ram_mb'])} GB · CPU: {os.environ.get('PROCESSOR_IDENTIFIER', '?')} ({os.cpu_count()} {L('luồng', 'threads')})")
    for root, free, total in drives():
        (ok if free > 20 else warn)(L(f"Ổ {root} còn trống {free:.0f} GB / {total:.0f} GB", f"Drive {root} {free:.0f} GB free of {total:.0f} GB"))
    return hw


# ----- phần mềm --------------------------------------------------------------------------------
def lms_exe() -> str | None:
    exe = shutil.which("lms")
    if exe:
        return exe
    p = LMS_HOME / "bin" / "lms.exe"
    return str(p) if p.is_file() else None


def lmstudio_app() -> Path | None:
    try:
        p = Path(json.loads((LMS_HOME / ".internal" / "app-install-location.json").read_text(encoding="utf-8"))["path"])
        if p.is_file():
            return p
    except (OSError, ValueError, KeyError):
        pass
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        for sub in ("Programs/LM Studio/LM Studio.exe", "LM Studio/LM Studio.exe"):
            p = Path(base) / sub
            if base and p.is_file():
                return p
    return None


def app_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq LM Studio.exe", "/NH"], capture_output=True, text=True, timeout=15).stdout
        return "LM Studio.exe" in out
    except (OSError, subprocess.SubprocessError):
        return False


def server_up() -> bool:
    try:
        with urllib.request.urlopen("http://localhost:1234/v1/models", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def wait_for(cond, seconds: int, label: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            print()
            return True
        print(f"\r    {label} {int(time.time() - t0)}s / {seconds}s ", end="", flush=True)
        time.sleep(2)
    print()
    return False


def check_software() -> bool:
    """Trả True nếu LM Studio dùng được (có lệnh lms và server chạy)."""
    step(2, L("Kiểm tra phần mềm cần thiết", "Checking required software"))
    build = sys.getwindowsversion().build
    ok(f"Windows {11 if build >= 22000 else sys.getwindowsversion().major} (build {build}) · Python {sys.version.split()[0]}")
    (ok if shutil.which("uv") else warn)("uv " + (L("đã có", "found") if shutil.which("uv") else L("chưa có trong PATH (chạy lại install.ps1 để cài)", "not on PATH (re-run install.ps1)")))
    (ok if shutil.which("nvidia-smi") else warn)(L("Driver NVIDIA (nvidia-smi) ", "NVIDIA driver (nvidia-smi) ") + (L("đã có", "found") if shutil.which("nvidia-smi")
        else L("chưa có – cần tự cài tại https://www.nvidia.com/drivers", "missing – install it yourself from https://www.nvidia.com/drivers")))

    if not lmstudio_app() and not lms_exe():
        bad(L("LM Studio chưa được cài.", "LM Studio is not installed."))
        if not shutil.which("winget"):
            info(L("Máy không có winget nên không tự cài được. Tải LM Studio tại https://lmstudio.ai rồi chạy lại.",
                   "winget is unavailable, so it cannot be installed automatically. Get LM Studio from https://lmstudio.ai and run setup again."))
            return False
        if not yes(L("Cài LM Studio bằng winget ngay bây giờ (khoảng 500 MB)?", "Install LM Studio with winget now (about 500 MB)?")):
            return False
        code = run_live(["winget", "install", "-e", "--id", WINGET_ID, "--accept-package-agreements", "--accept-source-agreements"])
        if code != 0 or not lmstudio_app():
            bad(L("Cài LM Studio không thành công. Hãy cài tay tại https://lmstudio.ai rồi chạy lại.",
                  "LM Studio installation failed. Install it manually from https://lmstudio.ai and run setup again."))
            return False
    ok(L("LM Studio đã cài", "LM Studio is installed") + (f": {lmstudio_app()}" if lmstudio_app() else ""))

    if not lms_exe():  # lệnh lms chỉ xuất hiện sau khi LM Studio chạy lần đầu
        warn(L("Chưa có lệnh lms – mở LM Studio lần đầu để nó tự tạo...", "The lms command is missing – opening LM Studio once so it gets created..."))
        app = lmstudio_app()
        if app:
            os.startfile(app)  # noqa: S606
        if not wait_for(lambda: lms_exe() is not None, 180, L("Đang chờ LM Studio khởi động", "Waiting for LM Studio")):
            bad(L("Vẫn chưa có lệnh lms. Mở LM Studio, hoàn tất màn hình chào mừng rồi chạy lại.",
                  "Still no lms command. Open LM Studio, finish the welcome screen and run setup again."))
            return False
    ok(L("Lệnh lms đã có", "The lms command is available"))

    if not server_up():
        info(L("Đang bật server LM Studio...", "Starting the LM Studio server..."))
        run_live([lms_exe() or "lms", "server", "start"])
        wait_for(server_up, 40, L("Đang chờ server", "Waiting for the server"))
    if server_up():
        ok(L("Server LM Studio đang chạy (cổng 1234)", "The LM Studio server is running (port 1234)"))
        return True
    warn(L("Chưa bật được server LM Studio. Có thể bật sau bằng nút \"Khởi động server\" trong AI Bench.",
           "Could not start the LM Studio server. You can start it later with the \"Start server\" button in AI Bench."))
    return True


# ----- nơi lưu model ---------------------------------------------------------------------------
def models_dir() -> Path:
    try:
        return Path(json.loads(SETTINGS.read_text(encoding="utf-8"))["downloadsFolder"])
    except (OSError, ValueError, KeyError):
        return LMS_HOME / "models"


def set_models_dir(path: Path) -> bool:
    """Ghi thư mục model mới vào cài đặt của LM Studio (giữ nguyên các cài đặt khác, có lưu bản sao)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        data = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.is_file() else {}
        if SETTINGS.is_file():
            shutil.copy2(SETTINGS, SETTINGS.with_suffix(".json.aibench-backup"))
        data["downloadsFolder"] = str(path)
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except (OSError, ValueError) as e:
        bad(str(e))
        return False


def choose_models_dir() -> Path:
    step(3, L("Nơi lưu model AI", "Where to store AI models"))
    cur = models_dir()
    ok(L(f"Hiện tại: {cur}  (ổ này còn trống {free_gb(cur):.0f} GB)", f"Current: {cur}  ({free_gb(cur):.0f} GB free on this drive)"))
    ds = drives()
    for i, (root, free, _total) in enumerate(ds, 1):
        info(f"{i}. {root}  {L('còn trống', 'free')} {free:.0f} GB")
    info(L("Model AI nặng từ 2 đến 60 GB mỗi cái. Nếu ổ hiện tại sắp đầy, hãy chọn ổ khác.",
           "AI models take 2 to 60 GB each. If the current drive is nearly full, pick another one."))
    a = ask(L("Enter = giữ nguyên · nhập số thứ tự ổ đĩa hoặc đường dẫn thư mục để đổi:",
              "Enter = keep · type a drive number or a folder path to change:"))
    if not a:
        return cur
    new = Path(ds[int(a) - 1][0]) / "LM Studio Models" if a.isdigit() and 1 <= int(a) <= len(ds) else Path(a.strip('"'))
    if not new.is_absolute():
        bad(L("Đường dẫn không hợp lệ, giữ nguyên thư mục cũ.", "Invalid path; keeping the current folder."))
        return cur
    if new.resolve() == cur.resolve():
        return cur
    if app_running():
        warn(L("LM Studio đang mở. Hãy thoát hẳn LM Studio (kể cả biểu tượng ở khay hệ thống) để cài đặt mới không bị ghi đè.",
               "LM Studio is running. Quit it completely (including the tray icon) so the new setting is not overwritten."))
        ask(L("Thoát LM Studio xong thì nhấn Enter...", "Press Enter once LM Studio is closed..."))
    if set_models_dir(new):
        ok(L(f"Model mới sẽ lưu tại: {new}", f"New models will be stored in: {new}"))
        info(L("Các model đã tải trước đó vẫn nằm ở thư mục cũ; muốn dùng tiếp thì chép sang thư mục mới.",
               "Models downloaded earlier stay in the old folder; copy them over if you still want them."))
        return new
    return cur


# ----- gợi ý model -----------------------------------------------------------------------------
def fit(size_gb: float, vram_mb: float, ram_mb: float) -> str:
    """ok / tight / slow / no. Cùng công thức với giao diện: file model +5% +300 MB + ngữ cảnh 8.192 token."""
    need = size_gb * 1024 * 1.05 + 300 + size_gb * 2.3 * 8
    if vram_mb:
        if need <= vram_mb - 1024:
            return "ok"
        if need <= vram_mb:
            return "tight"
    if need <= (vram_mb + ram_mb * 0.8 if vram_mb else ram_mb * 0.6):
        return "slow"
    return "no"


def installed_models() -> set[str]:
    exe = lms_exe()
    if not exe:
        return set()
    try:
        out = subprocess.run([exe, "ls", "--json"], capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace").stdout
        return {m.get("modelKey", "") for m in json.loads(out)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()


def suggest_models(hw: dict[str, Any], lm_ready: bool, mdir: Path) -> None:
    step(4, L("Gợi ý model AI theo cấu hình máy", "Suggested AI models for this PC"))
    vram, ram = hw["gpu"].get("vram_total_mb") or 0, hw["ram_mb"]
    catalog = json.loads((Path(__file__).parent / "catalog.json").read_text(encoding="utf-8"))["models"]
    have = installed_models()
    for m in catalog:
        m["fit"], m["have"] = fit(m["size_gb"], vram, ram), m["key"] in have
    good = [m for m in catalog if m["fit"] == "ok"]
    # Đề xuất: model thị giác lớn nhất còn vừa VRAM (chạy được cả 3 bài test); không có GPU thì model nhỏ nhất.
    pick = max((m for m in good if m["vision"]), key=lambda m: m["size_gb"], default=None) or (
        min((m for m in catalog if m["fit"] != "no"), key=lambda m: m["size_gb"], default=None))
    info(L(f"Cấu hình: VRAM {gb(vram)} GB · RAM {gb(ram)} GB", f"Hardware: {gb(vram)} GB VRAM · {gb(ram)} GB RAM"))
    label = {"ok": color("92", L("✓ phù hợp", "✓ fits")), "tight": color("93", L("△ vừa sát VRAM", "△ barely fits")),
             "slow": color("93", L("⚠ chạy được nhưng chậm", "⚠ runs but slowly")), "no": color("91", L("✗ quá lớn", "✗ too large"))}
    print()
    for i, m in enumerate(catalog, 1):
        star = color("96;1", "★") if m is pick else " "
        kind = L("chữ + ảnh", "text + vision") if m["vision"] else L("chỉ chữ  ", "text only  ")
        print(f"   {star} {i:2}. {m['name']:16} {m['size_gb']:5.1f} GB  {kind}  {label[m['fit']]}" + (L("  (đã có)", "  (installed)") if m["have"] else ""))
    print()
    info(L("★ = đề xuất. Model \"chữ + ảnh\" chạy được cả 3 bài test; \"chỉ chữ\" chỉ chạy Benchmark GPU.",
           "★ = recommended. \"text + vision\" models run all 3 tests; \"text only\" runs the GPU benchmark only."))
    info(L("Dung lượng là của bản mặc định trên LM Studio, có thể lệch đôi chút.", "Sizes are for LM Studio's default variant and may differ slightly."))
    if not lm_ready or not lms_exe():
        warn(L("LM Studio chưa sẵn sàng nên bỏ qua bước tải model. Chạy lại: uv run python -m aibench setup",
               "LM Studio is not ready, so model download is skipped. Re-run: uv run python -m aibench setup"))
        return
    default = "" if not pick or pick["have"] else str(catalog.index(pick) + 1)
    a = ask(L(f"Nhập số các model muốn tải, cách nhau dấu phẩy (Enter = {default or 'không tải'}, 0 = bỏ qua):",
              f"Type the numbers of the models to download, comma-separated (Enter = {default or 'none'}, 0 = skip):"), default)
    chosen = [catalog[int(x) - 1] for x in a.replace(" ", "").split(",") if x.isdigit() and 1 <= int(x) <= len(catalog)]
    chosen = [m for m in chosen if not m["have"]]
    if not chosen:
        info(L("Không tải model nào.", "No models to download."))
        return
    total, free = sum(m["size_gb"] for m in chosen), free_gb(mdir)
    risky = [m["name"] for m in chosen if m["fit"] in ("slow", "no")]
    if risky and not yes(L(f"{', '.join(risky)} không hợp cấu hình máy này (sẽ rất chậm hoặc không chạy được). Vẫn tải?",
                           f"{', '.join(risky)} does not suit this PC (very slow or will not run). Download anyway?"), False):
        chosen = [m for m in chosen if m["fit"] not in ("slow", "no")]
        total = sum(m["size_gb"] for m in chosen)
    if chosen and total + 2 > free:
        bad(L(f"Cần {total:.0f} GB nhưng {mdir} chỉ còn {free:.0f} GB. Chạy lại setup và chọn ổ khác ở bước 3.",
              f"{total:.0f} GB needed but {mdir} only has {free:.0f} GB free. Re-run setup and pick another drive in step 3."))
        return
    for i, m in enumerate(chosen, 1):
        print(color("96", f"\n  ↓ [{i}/{len(chosen)}] {m['name']} ({m['size_gb']:.1f} GB)  →  {mdir}"))
        code = run_live([lms_exe() or "lms", "get", m["key"], "--gguf", "-y"])
        (ok if code == 0 else bad)(m["name"] + (L(" đã tải xong", " downloaded") if code == 0 else L(" tải lỗi (thử lại: lms get ", " failed (retry: lms get ") + m["key"] + ")"))


# ----- hoàn tất --------------------------------------------------------------------------------
def make_shortcut() -> bool:
    ps = ("$d=[Environment]::GetFolderPath('Desktop');$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\\AI Bench.lnk\");"
          f"$s.TargetPath='{ROOT / 'run.bat'}';$s.WorkingDirectory='{ROOT}';$s.IconLocation='shell32.dll,12';$s.Save()")
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def finish() -> None:
    step(5, L("Hoàn tất", "Finishing"))
    if yes(L("Tạo lối tắt \"AI Bench\" ngoài Desktop?", "Create an \"AI Bench\" shortcut on the Desktop?")):
        (ok if make_shortcut() else warn)(L("Lối tắt ngoài Desktop", "Desktop shortcut"))
    ok(L(f"AI Bench đã cài tại: {ROOT}", f"AI Bench is installed in: {ROOT}"))
    info(L("Mở ứng dụng: bấm đúp run.bat (hoặc lối tắt AI Bench), rồi vào http://127.0.0.1:8765",
           "To start: double-click run.bat (or the AI Bench shortcut), then open http://127.0.0.1:8765"))
    info(L("Chạy lại trình cài đặt bất cứ lúc nào: uv run python -m aibench setup", "Re-run this setup any time: uv run python -m aibench setup"))
    if not YES and yes(L("Mở AI Bench ngay bây giờ?", "Start AI Bench now?")):
        os.startfile(ROOT / "run.bat")  # noqa: S606


def main(assume_yes: bool = False, lang: str | None = None) -> None:
    global EN, YES
    YES = assume_yes
    if sys.platform != "win32":
        sys.exit("AI Bench setup currently supports Windows only.")
    os.system("")  # bật màu ANSI trên console Windows cũ
    print(color("96;1", "\n  AI BENCH · Tech by Lân") + "  –  setup")
    EN = (lang or ask("Ngôn ngữ / Language: [1] Tiếng Việt  [2] English", "1")).lower() in ("2", "en", "english")
    hw = check_hardware()
    lm_ready = check_software()
    mdir = choose_models_dir() if lm_ready else models_dir()
    if not lm_ready:
        step(3, L("Nơi lưu model AI (bỏ qua vì LM Studio chưa sẵn sàng)", "Model folder (skipped: LM Studio is not ready)"))
    suggest_models(hw, lm_ready, mdir)
    finish()
