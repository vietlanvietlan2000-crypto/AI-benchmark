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

## Chatbot hỏi đáp tài liệu

Bấm **Chatbot** trên thanh đầu của giao diện.

1. Bỏ tài liệu vào thư mục `Tai_lieu/`, chia thư mục con tùy ý (ví dụ `Nhan su/`, `San pham/May X5/`).
   Đọc được: `.pdf` (có chữ hoặc bản scan), `.docx`, `.xlsx`, `.txt`, `.md`, `.csv` và file ảnh `.jpg`, `.png`, `.webp`, `.bmp`, `.tif`.
   Thư mục `Tai_lieu/Vi du/` là bộ tài liệu giả lập để thử, xóa đi khi dùng thật.
2. Chọn model lập chỉ mục (embedding) và model OCR, rồi bấm **Cập nhật chỉ mục**.
3. Chọn model trả lời và đặt câu hỏi.

**Lập chỉ mục gồm 5 bước** (giao diện hiện bước đang chạy, có nút **Dừng**):

| Bước | Việc làm |
|---|---|
| 1. Tiếp nhận | Quét thư mục; chỉ xử lý file mới hoặc đã sửa (đổi model embedding thì làm lại tất cả) |
| 2. Lấy chữ | PDF có chữ, Word, Excel, txt/md/csv: đọc thẳng. Trang PDF không có lớp chữ (bản scan) và file ảnh: OCR bằng model thị giác |
| 3. Cắt đoạn | Chia thành đoạn khoảng 900 ký tự, ghi đoạn nằm ở trang / mục / sheet nào |
| 4. Vector hóa | Model embedding đổi mỗi đoạn thành vector |
| 5. Lưu trữ | Ghi vào cơ sở dữ liệu SQLite `rag_index/index.db`, từng tài liệu một |

- **OCR:** dùng model thị giác trong LM Studio, nên chọn model chuyên OCR (ví dụ `chandra-ocr-2`); model thị giác thường (Gemma 4)
  cũng dùng được. Mỗi trang mất khoảng 5–20 giây, ứng dụng OCR 3 trang cùng lúc. Trong lúc OCR, các model khác được gỡ để nhường VRAM;
  OCR xong thì gỡ model OCR rồi mới nạp model embedding.
- **Kết quả OCR được lưu lại** theo nội dung file: đổi model embedding hay lập lại chỉ mục không phải OCR lại.
  Bấm Dừng giữa chừng thì các trang đã OCR và tài liệu đã lưu vẫn còn, lần sau làm tiếp.
- Chọn "Không OCR" thì trang scan bị bỏ qua; tài liệu đó hiện dấu ⚠ kèm số trang chưa OCR.
- Khi đang lập chỉ mục thì không chat và không chạy benchmark được (cùng dùng GPU).
- OCR vẫn có thể đọc sai (chữ viết tay, bản scan mờ). Trong khung xem nguồn, đoạn lấy từ OCR có nhãn **OCR** để biết mà kiểm tra lại.

Mỗi câu hỏi đi qua hai bước: tìm các đoạn gần câu hỏi nhất để biết **tài liệu nào liên quan** (tên thư mục và tên file cũng được
tính vào), rồi đưa cho AI **đọc trọn tài liệu đó**; tài liệu dài quá ngữ cảnh thì chỉ đưa các đoạn liên quan kèm đoạn liền kề.
Dưới mỗi câu trả lời có tên tài liệu AI đã đọc, bấm vào để mở file.

**Trích dẫn nguồn.** Mỗi ý trong câu trả lời có số nguồn dạng `[2]`. Dưới câu trả lời liệt kê từng nguồn kèm vị trí:
trang (PDF), mục theo tiêu đề (Word, Markdown), sheet (Excel). Bấm vào số hoặc dòng nguồn để mở khung xem tài liệu:
đoạn được trích dẫn có nền vàng, dòng khớp với câu trả lời được tô đậm hơn. Nút "Mở file gốc" mở file bằng ứng dụng mặc định.

**Bảng, Excel, biểu đồ.** Yêu cầu "lập bảng…" thì AI trả về bảng, dưới bảng có nút **Xuất Excel**. Yêu cầu "vẽ biểu đồ cột / đường / tròn…"
thì giao diện vẽ biểu đồ, nút **Xuất Excel (kèm biểu đồ)** tạo file có cả số liệu lẫn biểu đồ Excel.

**Tìm kiếm qua internet.** Tick "Tìm kiếm qua internet" thì câu hỏi được gửi tới máy tìm kiếm (thư viện `ddgs`, không cần khóa API),
AI đọc vài trang kết quả đầu và trả lời kèm link nguồn. Mặc định tắt vì câu hỏi sẽ rời khỏi máy. Nguồn tìm kiếm miễn phí có lúc
không phản hồi; khi đó hỏi lại sau ít giây. Có thể bật cùng lúc với tra cứu tài liệu.

Lưu ý:
- Ô "Model lập chỉ mục" liệt kê mọi model trong LM Studio. Nhóm trên dùng được ngay; model ở nhóm dưới bị LM Studio xếp
  là model chat, muốn dùng thì trong LM Studio mở My Models → ⚙ của model → "Override Domain Type" → "Embedding".
- Model embedding quyết định việc tìm đúng tài liệu. Với tiếng Việt nên dùng model đa ngôn ngữ (Qwen3 Embedding, bge-m3); `nomic-embed` yếu hơn.
- PDF scan (chỉ có ảnh) không đọc được, trong danh sách sẽ hiện dấu ⚠.
- Khi chatbot tự nạp model, nó nạp model trả lời với ngữ cảnh 16.384 token và model embedding với 4.096 token. Model đã nạp sẵn thì giữ nguyên ngữ cảnh đang có.
- Nút **Gỡ các model đã nạp** giải phóng VRAM; model cần dùng sẽ tự nạp lại ở câu hỏi kế tiếp. Nên bấm nút này khi model đã được nạp sẵn
  từ nơi khác với ngữ cảnh lớn (ví dụ Qwen3 Embedding 8B tự nạp với 40.960 token chiếm hơn 10 GB VRAM).
- AI vẫn có thể trả lời sai; tên tài liệu nguồn là để người dùng kiểm tra lại.
- Bỏ tick "Tra cứu tài liệu" để chat tự do với model. Hội thoại không được lưu lại.

Mã nguồn: `aibench/rag.py` (đọc file, OCR, chỉ mục, tìm kiếm), `aibench/chat.py` (API), `aibench/websearch.py` (tìm kiếm internet).
Chỉ mục và kết quả OCR lưu ở `rag_index/index.db`.

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

## Giấy phép

Miễn phí theo giấy phép [MIT](LICENSE): ai cũng được dùng, sửa và chia sẻ lại, kể cả cho mục đích thương mại, miễn là giữ dòng ghi bản quyền.
