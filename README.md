# UCircle AI Content Automation 🚀
## Hệ thống tự động thu thập tin tức, phân tích bằng AI, tạo ảnh minh họa và đăng lên UCircle

---

## Kiến trúc 2 Phase (Tách biệt thu thập & đăng bài)

```
PHASE 1 — COLLECTION (mỗi 20 phút, KHÔNG gọi AI)
══════════════════════════════════════════════════════
RSS (VnExpress / Thanh Niên / VietnamPlus...)
    ↓ Collector      → bóc tách bài mới từ RSS
    ↓ Normalizer     → làm sạch, lọc keyword Tier-1
    ↓ Deduplicator   → chặn URL & title trùng lặp
    ↓
  [SQLite: status=QUEUED] ← Hàng đợi bài viết


PHASE 2 — PUBLISH (mỗi 45 phút, trong khung 6h-23h)
══════════════════════════════════════════════════════
Chọn bài tốt nhất từ hàng đợi (trust_level × freshness)
    ↓ Gemini Analyzer → chấm điểm & trích xuất sự kiện
    ↓ Gemini Writer   → viết bản tóm tắt 80-250 từ
    ↓ Image Resolver  → ưu tiên ảnh đúng từ bài gốc; AI chỉ là dự phòng
    ↓ Validator       → kiểm duyệt 12 tiêu chí chất lượng
    ↓
  [SQLite: status=READY/REVIEW/REJECTED]
    ↓ (nếu READY + semi_auto/full_auto mode)
  UCircle Publisher  → Playwright đăng bài
    ↓
  Publish Log        → ghi nhận kết quả
```

---

## 🛠 Hướng dẫn cài đặt & Chạy

### 1. Cài đặt thư viện
```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Cấu hình biến môi trường
Sao chép `.env.example` thành `.env`:
```bash
cp .env.example .env
```
Điền `GEMINI_API_KEY` (lấy miễn phí tại [Google AI Studio](https://aistudio.google.com)).

### 3. Khởi tạo cơ sở dữ liệu
```bash
python scripts/init_db.py
```

### 4. Kiểm tra các module
```bash
# Kiểm tra đọc tin RSS:
python scripts/test_rss.py

# Kiểm tra kết nối Gemini AI:
python scripts/test_gemini.py
```

---

## 🚀 Các phương thức vận hành

### Cách 1: Sử dụng File Batch chạy nhanh (Khuyên dùng trên Windows)
- `run_dashboard.bat`: Mở giao diện Dashboard tại http://127.0.0.1:8000 (xem Hàng đợi, duyệt bài, kích hoạt thủ công).
- `run_collection.bat`: Chạy Phase 1 ngay lập tức (thu thập RSS -> hàng đợi, 0 Gemini call).
- `run_publish_now.bat`: Chạy Phase 2 ngay lập tức (chọn bài tốt nhất -> Gemini AI -> lưu để duyệt; chỉ tự đăng khi cấu hình `semi_auto` hoặc `full_auto`).
- `run_scheduler.bat`: Chạy tự động liên tục theo 2 Job nền độc lập.

---

### Cách 2: Chạy bằng dòng lệnh (CLI)

#### 1. Chạy Phase 1: Thu thập & Nạp hàng đợi (Mỗi 20 phút, 0 AI call)
```bash
python collection_pipeline.py
```

#### 2. Chạy Phase 2: Xử lý 1 bài tốt nhất & Đăng (Theo lịch)
```bash
# Chạy thử nghiệm (không đăng thật):
python publish_pipeline.py --dry-run

# Chạy đăng thật:
python publish_pipeline.py
```

#### 3. Chạy Toàn bộ Tự động với Scheduler (2 Job độc lập)
```bash
python scheduler.py
```

#### 4. Mở Giao diện Web Review Dashboard
```bash
python review_ui/app.py
```
👉 Truy cập: **http://127.0.0.1:8000** để xem hàng đợi bài viết (Tab Queue), duyệt bài AI đã tạo, hoặc bấm các nút kích hoạt nhanh.

### Luồng thao tác khuyên dùng trên Dashboard

1. Bấm **Thu thập tin mới ngay** để quét RSS và đưa tin mới vào hàng đợi.
2. Bấm **Tạo bài AI từ tin HOT** hoặc chọn một tin cụ thể trong hàng đợi.
3. Xem tổng thể, kiểm tra nguồn và ảnh; sau đó bấm **Duyệt (Ready)**.
4. Bấm **Đăng ngay** trên bài đã duyệt hoặc **Đăng bài READY tiếp theo**.

Ảnh bài đăng mặc định được lấy từ thẻ `og:image`/RSS của chính bài báo, chuẩn hóa về tỷ lệ 16:9 và ghi rõ nguồn. Nếu bài gốc không có ảnh hợp lệ, hệ thống mới tạo một ảnh AI dự phòng sát theo tiêu đề và tóm tắt.

---

## 📂 Cấu trúc thư mục

```
AutoUpFeed_tintuc24h/
├── config/
│   ├── sources.json          # Danh sách nguồn RSS & độ tin cậy
│   ├── circles.json          # Cấu hình Circle & Persona biên tập
│   └── automation.json       # Tần suất, giới hạn bài, điểm ngưỡng
├── data/
│   ├── automation.db         # SQLite database (6 bảng)
│   ├── images/               # Thư mục lưu ảnh AI
│   └── logs/                 # Nhật ký chạy hằng ngày
├── prompts/
│   ├── analyzer.md           # Prompt AI phân tích bài
│   ├── writer.md             # Prompt AI viết tóm tắt
│   ├── image_prompt.md       # Prompt AI sinh ảnh
│   └── validator.md          # Prompt AI kiểm duyệt chất lượng
├── src/
│   ├── collector.py          # Module 1: Thu thập RSS
│   ├── normalizer.py         # Module 2: Chuẩn hóa & lọc Tier 1
│   ├── deduplicator.py       # Module 3: Chống bài trùng 2 cấp độ
│   ├── analyzer.py           # Module 4: Phân tích nội dung
│   ├── writer.py             # Module 6: Viết bản tóm tắt UCircle
│   ├── image_generator.py    # Module 7+8: Xử lý ảnh AI
│   ├── validator.py          # Module 9: Thẩm định & chấm điểm
│   ├── queue_manager.py      # Quản lý hàng đợi & Rate limit
│   ├── publisher.py          # Module 10: Tự động đăng UCircle
│   ├── storage.py            # Quản lý SQLite database
│   └── logger.py             # Ghi nhận log & thống kê chu kỳ
├── review_ui/
│   ├── app.py                # FastAPI Web Server
│   ├── templates/            # Giao diện HTML
│   └── static/               # CSS glassmorphism & JS
├── scripts/
│   ├── init_db.py            # Tạo DB & seed nguồn tin
│   ├── test_rss.py           # Test RSS
│   └── test_gemini.py        # Test Gemini AI
├── pipeline.py               # Bộ điều phối Pipeline toàn trình
└── scheduler.py              # Bộ lập lịch chạy định kỳ
```
