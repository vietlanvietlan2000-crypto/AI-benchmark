# AI Bench

Phần mềm đo hiệu năng card đồ họa (VGA) khi chạy AI, dùng LM Studio làm nền chạy model.
Thiết kế theo dạng module để sau này thêm tính năng khác (phân tích email, chat, đọc ảnh X-quang...).

## Cài đặt (Windows 10/11)

Mở **PowerShell** và dán lệnh sau:

```powershell
irm https://raw.githubusercontent.com/vietlanvietlan2000-crypto/AI-benchmark/main/install.ps1 | iex
```

Trình cài đặt hiện tiến độ từng bước và hỏi bạn ở những chỗ cần chọn:

| Bước | Việc làm |
|---|---|
| Tải về | Chọn thư mục cài, tải mã nguồn, cài `uv`, Python và thư viện |
| 1. Phần cứng | Đọc GPU, VRAM, RAM, dung lượng trống từng ổ đĩa |
| 2. Phần mềm | Kiểm tra driver NVIDIA, LM Studio, lệnh `lms`, server LM Studio; thiếu LM Studio thì hỏi rồi cài bằng `winget` |
| 3. Nơi lưu model | Cho đổi thư mục model của LM Studio sang ổ khác khi ổ hiện tại sắp đầy |
| 4. Model | Gợi ý model hợp với VRAM/RAM của máy và tải những model bạn chọn |
| 5. Hoàn tất | Tạo lối tắt ngoài Desktop, mở ứng dụng |

Chạy lại lệnh trên để cập nhật bản mới (kết quả đo trong `results/` được giữ nguyên).
Chạy lại riêng phần kiểm tra máy và tải model: `uv run python -m aibench setup` (thêm `--lang en` để dùng tiếng Anh).
Danh sách model gợi ý nằm ở `aibench/catalog.json`. Driver NVIDIA không tự cài được, cần tải tại https://www.nvidia.com/drivers.

## Chạy

Cần có: LM Studio (kèm lệnh `lms`), [uv](https://docs.astral.sh/uv/), driver NVIDIA (`nvidia-smi`) — trình cài đặt ở trên lo các thứ này.

- **Giao diện web:** bấm đúp `run.bat`, hoặc `uv run python -m aibench serve` rồi mở http://127.0.0.1:8765
- **Dòng lệnh:**

```
uv run python -m aibench models
uv run python -m aibench modules
uv run python -m aibench run gpu_benchmark -p model=openai/gpt-oss-20b -p runs=3
uv run python -m aibench run vision_benchmark -p model=google/gemma-4-e4b
```

Nếu server LM Studio chưa bật: bấm nút "Khởi động server" trên giao diện, hoặc chạy `lms server start`.

Kết quả mỗi lần chạy lưu thành file JSON trong `results/` và hiện ở mục Lịch sử.

## Điện năng tiêu thụ

Mọi chức năng đều đo lượng điện GPU tiêu thụ (Wh), tính bằng cách cộng dồn công suất × thời gian từ các mẫu
`nvidia-smi` lấy mỗi 0,5 giây:

| Chức năng | Chỉ số điện |
|---|---|
| Mọi module (tự động) | Điện GPU tiêu thụ cả lần chạy, kể cả lúc nạp model |
| Benchmark GPU | Điện từng kịch bản, điện mỗi 1.000 token |
| Benchmark hình ảnh | Điện từng độ phân giải, điện mỗi ảnh |
| Phân tích hóa đơn | Điện từng hóa đơn (ghi dưới mỗi ảnh), điện trung bình mỗi hóa đơn |

Thanh trên cùng của giao diện hiện công suất GPU tức thời (W). Module mới tự có chỉ số "cả lần chạy";
muốn đo riêng một đoạn thì dùng `mark = ctx.gpu.mark()` rồi `ctx.gpu.energy_wh(mark)`.

Đây chỉ là điện của card đồ họa, **không gồm CPU, màn hình và phần còn lại của máy**. 1.000 Wh = 1 kWh = 1 số điện.

## Benchmark GPU đo những gì

| Kịch bản | Mục đích |
|---|---|
| Sinh ngắn | Tốc độ sinh (tok/s) và độ trễ token đầu tiên (TTFT) |
| Ngữ cảnh dài | Tốc độ GPU đọc prompt ~2700 token |
| Sinh dài | Tốc độ khi chạy ổn định, kèm % GPU, nhiệt độ, công suất |
| Song song ×N | Tổng thông lượng khi nhiều yêu cầu chạy cùng lúc |

**Điểm tổng** = 10 × trung bình nhân của (tok/s sinh ngắn, tok/s sinh dài, tok/s song song, tok/s đọc prompt ÷ 10).
Điểm chỉ so sánh được giữa các lần chạy **cùng model, cùng tham số**.

Lưu ý khi đọc số:
- Tốc độ đọc prompt là xấp xỉ (số token prompt ÷ TTFT).
- Thời gian nạp model chỉ đo khi model chưa nạp sẵn lúc bấm Chạy.
- Với model "suy nghĩ" (gpt-oss, qwen), token suy nghĩ cũng được tính là token sinh ra.
- Model lớn hơn VRAM sẽ tràn sang RAM/CPU và chậm hẳn — xem cột VRAM đỉnh và % GPU để nhận biết.

## Benchmark phân tích hình ảnh đo những gì

Phần mềm tự vẽ ảnh test vuông ở nhiều độ phân giải (mặc định 448, 896, 1344, 2048 px) và gửi tới model thị giác.
Mỗi ảnh có nội dung ngẫu nhiên đã biết đáp án, nên chấm được cả tốc độ lẫn độ chính xác:

| Trong ảnh | Model phải làm | Kiểm tra |
|---|---|---|
| 3–8 hình tròn màu | Đếm số hình | Khả năng nhận biết vật thể |
| Mã lớn 6 ký tự | Đọc lại mã | Đọc chữ (OCR) |
| Dãy 5 chữ số nhỏ ở đáy ảnh | Đọc lại dãy số | Nhìn chi tiết nhỏ |

Kết quả gồm: số ảnh/phút, thời gian tới token đầu (gồm thời gian mã hoá ảnh), số token mỗi ảnh chiếm,
tỉ lệ đúng từng loại câu hỏi, VRAM, nhiệt độ, công suất.

**Điểm tổng** = số ảnh/phút × tỉ lệ trả lời đúng.

Trên giao diện, từng ảnh hiện ra ngay lúc AI đang xem, kèm câu trả lời của AI so với đáp án (✓/✗).
Các ảnh này được lưu cùng kết quả nên xem lại được trong Lịch sử; bấm vào ảnh để phóng to.
Module khác cũng dùng được cơ chế này: gọi `ctx.show(...)` khi đang chạy và trả về `images` trong kết quả.

Lưu ý: tốc độ phụ thuộc nhiều vào việc model có "suy nghĩ" trước khi trả lời hay không, nên chỉ so sánh
giữa các GPU khi dùng cùng model. Ảnh test là ảnh tổng hợp đơn giản, không đại diện cho ảnh y tế thật.

## Phân tích hóa đơn bán lẻ

| | Thư mục |
|---|---|
| Ảnh hóa đơn (jpg, png, webp, bmp, tif, gif) | `Invoice_Ana/Raw_invoice` |
| File Excel mẫu (dùng file `.xlsx` đầu tiên, dòng 1 là tiêu đề cột) | `Invoice_Ana/Template invoice` |
| File kết quả `Thong ke hoa don <ngày-giờ>.xlsx` | `Invoice_Ana/Ket qua` |

Chọn "Phân tích hóa đơn bán lẻ" trên giao diện rồi bấm Chạy. Ô "Số lượng hóa đơn cần chạy" giới hạn số ảnh
(0 = tất cả, theo thứ tự tên file); các nút "Chạy 10 / 20 / 30 hóa đơn" điền số và chạy luôn. Hoặc dùng dòng lệnh
(thêm `-p limit=10` để giới hạn):

```
uv run python -m aibench run invoice_analysis -p model=qwen/qwen3.8-27b
```

Model thị giác đọc từng ảnh và trả về JSON; phần mềm hiện bảng thống kê lên màn hình và ghi đúng bảng đó
vào bản sao của file mẫu. Mỗi dòng hàng hóa là 1 dòng; thông tin chung của hóa đơn lặp lại ở từng dòng.
Mỗi lần chạy tạo 1 file Excel mới, không ghi đè file cũ.

Thứ tự cột ghi ra cố định theo file mẫu hiện tại (A→R: mã hóa đơn, bên bán, bên mua, hình thức thanh toán,
dòng hàng, tổng tiền bằng chữ). Nếu đổi thứ tự cột trong file mẫu thì phải sửa `to_rows()` trong
`aibench/modules/invoice_analysis.py`.

**Phải kiểm tra lại kết quả.** Chữ in và con dấu đọc khá đúng, nhưng chữ viết tay vẫn sai đáng kể,
nhất là với model nhỏ. Ảnh chụp/scan càng nét thì càng đúng.

## Cấu trúc

```
aibench/
  core.py              Module, Param, Context, tự nạp module, chạy và lưu kết quả
  lmstudio.py          Gọi LM Studio (stream, đo TTFT/tok/s), nạp/gỡ model qua lệnh lms
  gpu.py               Lấy mẫu GPU bằng nvidia-smi (%, VRAM, nhiệt độ, công suất)
  server.py            API + chạy job nền (FastAPI)
  __main__.py          Dòng lệnh
  web/index.html       Giao diện (tự vẽ form và kết quả theo mô tả của module)
  modules/
    gpu_benchmark.py   Module benchmark GPU
    vision_benchmark.py  Module benchmark phân tích hình ảnh
    invoice_analysis.py  Module phân tích hóa đơn bán lẻ → Excel
Invoice_Ana/           Ảnh hóa đơn, file Excel mẫu, file kết quả
results/               Kết quả đã lưu
```

Cấu hình qua biến môi trường: `AIBENCH_LM_URL` (mặc định `http://localhost:1234`), `AIBENCH_GPU_INDEX` (mặc định `0`).

## Thêm tính năng mới

Tạo 1 file trong `aibench/modules/`, không cần sửa server hay giao diện:

```python
# aibench/modules/email_analysis.py
from ..core import Context, Module, Param

class EmailAnalysis(Module):
    id = "email_analysis"
    name = "Phân tích email"
    description = "Tóm tắt và phân loại một email."
    category = "app"
    params = [
        Param("model", "Model", "model"),
        Param("email", "Nội dung email", "textarea", ""),
    ]

    async def run(self, ctx: Context, p: dict) -> dict:
        s = await ctx.lm.chat(p["model"], [
            {"role": "system", "content": "Tóm tắt email, nêu mức độ khẩn cấp và việc cần làm."},
            {"role": "user", "content": p["email"]},
        ], max_tokens=1024)
        return {
            "summary": [{"label": "Tốc độ", "value": round(s.gen_tps, 1), "unit": "tok/s"}],
            "text": s.text,
        }

MODULE = EmailAnalysis()
```

Kiểu tham số hiện có: `model`, `int`, `bool`, `text`, `textarea`, `select`.
`run()` trả về `summary` (ô số liệu), `tables`, `charts`, `text` — giao diện tự hiển thị.

Với phân tích ảnh (X-quang): `ctx.lm.chat()` nhận `messages` chuẩn OpenAI nên gửi được ảnh dạng
`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}` tới model thị giác
(các model ghi "thị giác" trong danh sách). Việc còn thiếu là thêm kiểu tham số `image` (ô tải ảnh) vào giao diện.
