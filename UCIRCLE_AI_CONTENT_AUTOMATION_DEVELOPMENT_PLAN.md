# AI CONTENT AUTOMATION FOR UCIRCLE
## Quy trình phát triển hệ thống tự động tìm kiếm, phân tích, tổng hợp nội dung, tạo ảnh AI và đăng bài lên UCircle

**Phiên bản:** 1.0  
**Trạng thái:** Development Blueprint  
**Mục tiêu:** Xây dựng một hệ thống AI Automation có khả năng tự động thu thập bài viết từ các nguồn đáng tin cậy, phân tích và lọc nội dung, tạo bản tin tóm tắt mới, tạo ảnh minh họa bằng AI, lưu dữ liệu, sau đó đăng bài lên Circle được chỉ định trên UCircle.

---

# 1. Mục tiêu hệ thống

Hệ thống được xây dựng để tự động hóa toàn bộ quy trình:

```text
Nguồn tin chính thống
        ↓
Thu thập bài mới
        ↓
Chuẩn hóa dữ liệu
        ↓
Loại bài trùng
        ↓
AI phân tích
        ↓
AI kiểm tra mức độ phù hợp
        ↓
AI tóm tắt / viết lại
        ↓
AI tạo prompt ảnh
        ↓
Gemini tạo ảnh minh họa mới
        ↓
Kiểm tra bài + ảnh
        ↓
Lưu Draft
        ↓
Publish Queue
        ↓
UCircle
        ↓
Lưu Publish Log
```

Mục tiêu cuối cùng là tạo ra các bài đăng ngắn gọn, dễ đọc, có nguồn rõ ràng và có đường dẫn để người dùng truy cập bài viết gốc nếu muốn đọc chi tiết.

---

# 2. Nguyên tắc nội dung

Hệ thống KHÔNG hoạt động theo kiểu:

```text
Lấy bài báo
→ copy nguyên nội dung
→ copy ảnh
→ đăng lại
```

Hệ thống phải hoạt động theo kiểu:

```text
Bài báo gốc
→ AI đọc và hiểu
→ trích xuất thông tin chính
→ kiểm tra
→ viết một bản tóm tắt mới
→ tạo ảnh minh họa mới
→ dẫn nguồn
→ gắn link bài gốc
→ đăng UCircle
```

Mỗi bài đăng phải được xem là:

> Bản tin tóm tắt AI có dẫn nguồn.

---

# 3. Kiến trúc tổng thể

```text
                    INTERNET
                       │
               OFFICIAL SOURCES
                       │
          ┌────────────┴────────────┐
          │                         │
         RSS                       API
          │                         │
          └────────────┬────────────┘
                       ↓
                SOURCE COLLECTOR
                       ↓
                  NORMALIZER
                       ↓
                 DEDUPLICATOR
                       ↓
                 AI ANALYZER
                       ↓
             STORY / TOPIC FILTER
                       ↓
                 FACT CHECK
                       ↓
                  AI WRITER
                       ↓
           IMAGE PROMPT GENERATOR
                       ↓
            GEMINI IMAGE GENERATOR
                       ↓
               CONTENT VALIDATOR
                       ↓
                   STORAGE
                       ↓
                PUBLISH QUEUE
                       ↓
              UCIRCLE PUBLISHER
                       ↓
                 PUBLISH LOG
```

---

# 4. Công nghệ đề xuất

Ưu tiên tối đa chi phí 0 đồng hoặc Free Tier.

| Thành phần | Công nghệ đề xuất |
|---|---|
| Workflow Engine | n8n Community Self-hosted |
| AI Text | Gemini API |
| AI Image | Gemini Image Generation |
| Database V1 | SQLite hoặc JSON local |
| Database V2 | Supabase PostgreSQL |
| File Storage | Local / Supabase Storage |
| Scheduler | n8n |
| Nguồn tin | RSS / API chính thống |
| Publisher | UCircle API nếu có |
| Browser Automation dự phòng | Playwright |
| Logs | SQLite / Supabase |

---

# 5. Chi phí mục tiêu

Phiên bản MVP được thiết kế để có thể vận hành gần 0 đồng.

```text
n8n Community       → 0đ
RSS                  → 0đ
Gemini Free Tier     → 0đ trong hạn mức
SQLite               → 0đ
Local Storage        → 0đ
Scheduler            → 0đ
UCircle API          → phụ thuộc UCircle
```

Không cần mua ngay:

- OpenAI API
- VPS
- Supabase Pro
- Search API
- Kho ảnh trả phí
- Database server riêng

---

# 6. Phạm vi MVP đầu tiên

Không xây nhiều chủ đề ngay từ đầu.

MVP chỉ cần:

```text
1 Circle
1 chủ đề
3–5 nguồn tin
1 workflow
1 loại template bài đăng
1 tài khoản UCircle
```

Ví dụ:

```text
Circle:
Tin Tức 24h

Nguồn:
- VnExpress
- Thanh Niên
- VietnamPlus
- Báo Chính phủ
```

Sau khi workflow hoạt động ổn định mới mở rộng.

---

# 7. Source Registry

Hệ thống phải có danh sách nguồn được phép sử dụng.

Ví dụ:

```json
{
  "id": "source_001",
  "name": "VnExpress",
  "domain": "vnexpress.net",
  "rss_url": "https://example.com/rss",
  "category": "news",
  "trust_level": 90,
  "enabled": true,
  "requires_attribution": true,
  "image_policy": "DO_NOT_COPY_DEFAULT"
}
```

Các trường cần có:

```text
id
name
domain
rss_url
category
source_type
trust_level
requires_attribution
image_policy
enabled
```

Mục đích:

- chỉ lấy tin từ nguồn được cấu hình
- biết nguồn nào đáng tin
- biết nguồn nào bắt buộc ghi nguồn
- tránh lấy nội dung từ website không rõ nguồn gốc

---

# 8. Module 1 — Source Collector

## Mục tiêu

Thu thập bài viết mới từ các nguồn đã được cấu hình.

## Cách hoạt động

Ví dụ mỗi 15 phút:

```text
Schedule Trigger
      ↓
Get Enabled Sources
      ↓
Read RSS
      ↓
Extract New Articles
```

Một bài thu được:

```json
{
  "source_name": "VnExpress",
  "source_url": "https://...",
  "title": "...",
  "description": "...",
  "published_at": "...",
  "category": "technology"
}
```

---

# 9. Module 2 — Normalizer

Nguồn khác nhau sẽ trả dữ liệu khác nhau.

Normalizer chuẩn hóa tất cả về cùng một format.

Ví dụ:

```json
{
  "source_id": "source_001",
  "source_name": "VnExpress",
  "source_url": "https://...",
  "original_title": "...",
  "original_description": "...",
  "published_at": "...",
  "category": "...",
  "collected_at": "..."
}
```

---

# 10. Module 3 — Duplicate Detection

Không được để cùng một sự kiện bị đăng nhiều lần.

Ví dụ:

```text
VnExpress:
Apple giới thiệu sản phẩm ABC

Thanh Niên:
Apple chính thức ra mắt ABC

VietnamPlus:
Sản phẩm ABC mới của Apple xuất hiện
```

Ba bài trên có thể là cùng một sự kiện.

Hệ thống phải xử lý:

```text
Article A
Article B
Article C
   ↓
Similarity Check
   ↓
Same Story
   ↓
STORY-0001
```

Có 2 cấp độ chống trùng.

## Cấp độ 1 — URL Duplicate

Nếu `source_url` đã tồn tại:

```text
STOP
```

## Cấp độ 2 — Semantic Duplicate

So sánh:

```text
title
summary
keywords
entities
```

Nếu nội dung quá giống bài đã xử lý:

```text
DUPLICATE
```

Không đăng lại.

---

# 11. Module 4 — AI Analyzer

Gemini nhận dữ liệu bài báo và phân tích.

Input:

```text
title
description
source
published_at
category
source_url
```

Output nên ở dạng JSON:

```json
{
  "topic": "technology",
  "relevance_score": 92,
  "trust_score": 95,
  "freshness_score": 96,
  "is_sensitive": false,
  "publish": true,
  "reason": "Tin mới, nguồn rõ ràng và phù hợp Circle."
}
```

---

# 12. Rule chấm điểm

Có thể dùng thang điểm 0–100.

Ví dụ:

```text
Source Trust        25 điểm
Freshness           20 điểm
Topic Relevance     25 điểm
Content Quality     15 điểm
Uniqueness          15 điểm
```

Tổng:

```text
>= 85
→ READY

70–84
→ REVIEW

< 70
→ REJECT
```

---

# 13. Module 5 — Fact Check

Nếu có nhiều nguồn cho cùng sự kiện, hệ thống có thể đối chiếu.

Ví dụ:

```text
Nguồn A → 10 người
Nguồn B → 12 người
```

Không được tự chọn một số.

Chuyển:

```text
FACT_CONFLICT
```

và:

```text
status = NEEDS_REVIEW
```

AI không được:

- bịa số liệu
- tự suy đoán
- tự sửa số
- tự tạo quote
- tự kết luận nguyên nhân chưa có trong nguồn

---

# 14. Module 6 — AI Writer

AI Writer tạo bài đăng UCircle mới.

Không sao chép nguyên bài báo.

## Độ dài đề xuất

```text
80–250 từ
```

## Template chuẩn

```text
🔥 {{ai_title}}

{{summary_paragraph_1}}

{{summary_paragraph_2}}

📌 Điểm chính:
• {{point_1}}
• {{point_2}}
• {{point_3}}

🔗 Xem chi tiết:
{{source_url}}

📰 Nguồn: {{source_name}}
🖼 Ảnh minh họa AI

{{hashtags}}
```

---

# 15. Ví dụ bài đăng

```text
🔥 Apple giới thiệu tính năng AI mới trên điện thoại

Apple vừa công bố thêm một số tính năng AI mới dành cho thiết bị di động, tập trung vào hỗ trợ người dùng xử lý nội dung và tương tác thông minh hơn trong quá trình sử dụng hàng ngày.

Các tính năng mới được kỳ vọng giúp tối ưu các thao tác phổ biến như tìm kiếm, xử lý văn bản và hỗ trợ cá nhân.

📌 Điểm chính:
• Bổ sung khả năng AI mới
• Tập trung cải thiện trải nghiệm người dùng
• Hướng tới tương tác thông minh hơn

🔗 Xem chi tiết:
https://example.com/original-article

📰 Nguồn: Example News
🖼 Ảnh minh họa AI

#CongNghe #TinTuc #AI
```

---

# 16. Rule cho AI Writer

AI được phép:

```text
- tóm tắt
- viết lại
- rút gọn
- sắp xếp ý
- tạo tiêu đề
- tạo hashtag
```

AI không được phép:

```text
- bịa dữ liệu
- thêm thông tin không có trong nguồn
- giả quote
- thay đổi số liệu
- tự suy đoán nguyên nhân
- sao chép nhiều đoạn dài nguyên văn
```

---

# 17. Module 7 — Image Prompt Generator

Sau khi bài viết hoàn chỉnh:

```text
AI Writer
      ↓
Image Prompt Generator
```

AI đọc:

```text
title
summary
topic
sensitivity
```

sau đó tạo prompt ảnh.

Ví dụ:

```text
Tạo ảnh minh họa công nghệ hiện đại cho một bài tin ngắn.
Chủ thể là smartphone và giao diện trí tuệ nhân tạo.
Bố cục sạch, hiện đại, không có watermark.
Không sao chép hình ảnh nguồn.
Không tạo chữ trên ảnh.
Ảnh dùng để minh họa, không phải ảnh sự kiện thực tế.
```

---

# 18. Module 8 — Gemini Image Generator

Gemini nhận prompt và tạo ảnh hoàn toàn mới.

Flow:

```text
Article
   ↓
AI Summary
   ↓
Image Prompt
   ↓
Gemini Image
   ↓
Save Image
```

Mục tiêu:

- không dùng trực tiếp ảnh bài báo
- giảm phụ thuộc nguồn ảnh
- đồng nhất phong cách Circle
- tạo thumbnail riêng cho UCircle

---

# 19. Quy tắc ảnh AI

## Có thể auto tạo ảnh cho:

```text
- công nghệ
- bóng đá tổng hợp
- đời sống
- mẹo hay
- giáo dục
- du lịch
- kiến thức
- giải trí
```

## Cần thận trọng:

```text
- tai nạn
- pháp luật
- thiên tai
- xung đột
- thương vong
- chính trị
- sự kiện nhạy cảm
```

Với nội dung nhạy cảm:

```text
Không tạo ảnh hiện trường giả.
```

Chỉ dùng:

```text
- infographic
- ảnh biểu tượng
- bản đồ
- cảnh trung tính
- hình minh họa khái niệm
```

Nếu ảnh có thể khiến người đọc hiểu nhầm là ảnh thật:

```text
NEEDS_REVIEW
```

---

# 20. Nhãn ảnh

Nếu dùng ảnh AI, bài đăng nên có:

```text
🖼 Ảnh minh họa AI
```

hoặc:

```text
🖼 Ảnh minh họa được tạo bởi AI.
```

Với nội dung nhạy cảm:

```text
🖼 Ảnh minh họa AI, không phải ảnh hiện trường.
```

---

# 21. Module 9 — Content Validator

Trước khi đăng:

```text
AI Writer
      ↓
Image Generator
      ↓
Content Validator
```

Validator kiểm tra:

```text
□ Có nguồn không?
□ Có source_url không?
□ Link nguồn có hợp lệ không?
□ Bài có trùng không?
□ Tin có quá cũ không?
□ Nội dung AI có khác nguồn không?
□ Có số liệu chưa xác minh không?
□ Có quote lạ không?
□ Có hashtag không?
□ Ảnh có đúng chủ đề không?
□ Ảnh có gây hiểu nhầm không?
□ Bài có quá dài không?
```

Nếu fail:

```text
REJECT
```

hoặc:

```text
NEEDS_REVIEW
```

Nếu pass:

```text
READY_TO_PUBLISH
```

---

# 22. Storage

## V1 đơn giản

Có thể dùng:

```text
SQLite
+
Local Folder
```

Ví dụ:

```text
data/
├── automation.db
├── sources.json
├── images/
├── drafts/
└── logs/
```

## V2

Chuyển sang:

```text
Supabase PostgreSQL
+
Supabase Storage
```

---

# 23. Database Model

Các bảng đề xuất:

```text
sources
articles
stories
generated_posts
media_assets
publish_logs
```

---

# 24. Bảng sources

```text
id
name
domain
rss_url
trust_level
requires_attribution
image_policy
enabled
created_at
```

---

# 25. Bảng articles

```text
id
source_id
source_url
original_title
original_description
published_at
collected_at
status
content_hash
```

---

# 26. Bảng stories

```text
id
topic
main_title
confidence_score
sensitivity_level
status
created_at
```

Một story có thể chứa nhiều articles.

---

# 27. Bảng generated_posts

```text
id
story_id
ai_title
ai_summary
bullet_points
hashtags
source_name
source_url
status
created_at
```

Status:

```text
DRAFT
READY
REVIEW
REJECTED
PUBLISHED
FAILED
```

---

# 28. Bảng media_assets

```text
id
post_id
type
prompt
file_path
is_ai_generated
validation_status
created_at
```

---

# 29. Bảng publish_logs

```text
id
post_id
circle_id
platform
publish_status
platform_post_id
published_at
error_message
```

---

# 30. Publish Queue

Không nên để AI tạo xong là đăng ngay.

Nên có:

```text
generated_posts
      ↓
READY_TO_PUBLISH
      ↓
Publish Queue
      ↓
UCircle Publisher
```

Nhờ vậy có thể:

- giới hạn tốc độ
- tránh spam
- retry khi lỗi
- kiểm tra trạng thái
- quản lý nhiều Circle

---

# 31. Module 10 — UCircle Publisher

Input:

```json
{
  "circle_id": "...",
  "title": "...",
  "content": "...",
  "image_path": "...",
  "source_url": "..."
}
```

Output:

```json
{
  "success": true,
  "post_id": "...",
  "published_at": "..."
}
```

Ưu tiên:

```text
UCircle API
```

Nếu API chưa có:

```text
Publisher Adapter
```

có thể tạm dùng Playwright để tự động thao tác trình duyệt.

Tuy nhiên kiến trúc hệ thống không được phụ thuộc hoàn toàn vào UI automation.

Nên tách:

```text
UCirclePublisher
```

thành module riêng.

---

# 32. Workflow n8n MVP

```text
Schedule Trigger
       ↓
Load Sources
       ↓
RSS Feed Read
       ↓
Normalize Data
       ↓
Check URL Exists
       ↓
        ├─ YES → STOP
        │
        └─ NO
             ↓
       Save Article
             ↓
       Basic Keyword Filter
             ↓
       Gemini Analyzer
             ↓
       Publish?
        │
        ├─ NO → REJECT
        │
        └─ YES
             ↓
       Duplicate Check
             ↓
       Gemini Writer
             ↓
       Image Prompt Builder
             ↓
       Gemini Image Generation
             ↓
       Content Validator
             ↓
       Save Draft
             ↓
       Ready?
        │
        ├─ NO → REVIEW
        │
        └─ YES
             ↓
       Publish Queue
             ↓
       UCircle Publisher
             ↓
       Save Publish Log
```

---

# 33. Tối ưu Gemini để tiết kiệm Free Tier

Không gửi mọi bài cho Gemini ngay.

Ví dụ:

```text
100 bài RSS
      ↓
Lọc URL
      ↓
80
      ↓
Lọc keyword bằng code
      ↓
25
      ↓
Lọc bài cũ
      ↓
15
      ↓
Gemini Analyzer
      ↓
5 bài tốt
      ↓
Gemini Writer
      ↓
5 bài
      ↓
Gemini Image
```

Không làm:

```text
100 bài
→ Gemini phân tích cả 100
→ Gemini viết cả 100
→ Gemini tạo 100 ảnh
```

---

# 34. Các tầng xử lý AI

Có thể chia thành:

```text
Tier 1:
Lọc bằng code
- URL
- date
- keywords
- category

Tier 2:
Gemini Analyze
- relevance
- quality
- sensitivity
- duplicate semantic

Tier 3:
Gemini Writer
- title
- summary
- bullets
- hashtags

Tier 4:
Gemini Image
- image prompt
- generated image
```

---

# 35. Chế độ vận hành

Hệ thống nên có 3 mode.

## MODE 1 — MANUAL REVIEW

```text
AI tạo Draft
→ người dùng duyệt
→ đăng
```

Dùng trong thời gian đầu.

Mục tiêu:

```text
50–100 bài test
```

---

## MODE 2 — SEMI AUTO

```text
confidence >= 90
→ auto publish

70–89
→ review

< 70
→ reject
```

---

## MODE 3 — FULL AUTO

```text
Collect
→ Analyze
→ Verify
→ Write
→ Generate Image
→ Validate
→ Publish
→ Log
```

Chỉ bật khi hệ thống đã ổn định.

---

# 36. Multi-Circle Architecture

Sau khi Circle Tin Tức 24h chạy ổn, có thể dùng chung engine cho nhiều Circle.

Ví dụ:

```json
{
  "circle_id": "circle_news",
  "circle_name": "Tin Tức 24h",
  "topics": [
    "thời sự",
    "công nghệ",
    "đời sống"
  ],
  "posts_per_day": 20,
  "auto_publish_score": 92
}
```

Circle bóng đá:

```json
{
  "circle_id": "circle_football",
  "circle_name": "Nét đẹp bóng đá",
  "topics": [
    "football",
    "premier league",
    "champions league",
    "vietnam football"
  ],
  "posts_per_day": 15,
  "auto_publish_score": 90
}
```

---

# 37. Circle Persona

Mỗi Circle có prompt riêng.

## Tin Tức 24h

```text
Tone:
- trung lập
- ngắn gọn
- rõ thông tin
- không giật tít
```

## Nét đẹp bóng đá

```text
Tone:
- năng động
- cảm xúc
- dễ đọc
- emoji vừa phải
```

## Đà Nẵng ăn gì

```text
Tone:
- thân thiện
- gần gũi
- mô tả trải nghiệm
- dễ khám phá
```

---

# 38. Logging

Mỗi bước phải lưu log.

Ví dụ:

```text
21/09/2026

Collected          320
URL Duplicate       80
Filtered            150
AI Analyzed          90
Rejected             55
Draft Generated      35
Review Pending        5
Published            30
Failed                0
```

---

# 39. Log từng bài

Ví dụ:

```text
POST #000521

Source:
VnExpress

Collected:
13:20

AI Analyze:
13:21

Confidence:
94

Draft Generated:
13:22

Image Generated:
13:23

Validation:
PASS

Circle:
Tin Tức 24h

Publish:
SUCCESS

UCircle Post ID:
xxxx
```

---

# 40. Retry Strategy

Nếu publish thất bại:

```text
FAILED
↓
retry_count + 1
↓
Retry
```

Ví dụ:

```text
Retry 1 → sau 1 phút
Retry 2 → sau 5 phút
Retry 3 → sau 15 phút
```

Sau 3 lần:

```text
PERMANENT_FAILED
```

và chờ xử lý thủ công.

---

# 41. Rate Limit

Không được publish quá nhanh.

Ví dụ:

```text
max_posts_per_hour = 4
max_posts_per_day = 30
minimum_interval = 10 phút
```

Các giá trị này phải nằm trong config.

---

# 42. File cấu hình hệ thống

Ví dụ:

```json
{
  "workflow": {
    "interval_minutes": 15
  },
  "ai": {
    "provider": "gemini",
    "min_publish_score": 85
  },
  "publishing": {
    "auto_publish": false,
    "min_interval_minutes": 10,
    "max_posts_per_day": 20
  }
}
```

---

# 43. Cấu trúc thư mục nếu phát triển thành project

```text
ucircle-ai-content/
│
├── README.md
│
├── docker-compose.yml
│
├── .env
│
├── config/
│   ├── sources.json
│   ├── circles.json
│   └── automation.json
│
├── data/
│   ├── automation.db
│   ├── images/
│   ├── drafts/
│   └── logs/
│
├── prompts/
│   ├── analyzer.md
│   ├── writer.md
│   ├── image_prompt.md
│   └── validator.md
│
├── workflows/
│   └── news_automation.json
│
├── scripts/
│   ├── normalize.py
│   ├── deduplicate.py
│   └── publisher.py
│
└── docs/
    ├── ARCHITECTURE.md
    ├── DATABASE.md
    └── DEVELOPMENT_PLAN.md
```

---

# 44. Environment Variables

Không hard-code API key.

Ví dụ:

```env
GEMINI_API_KEY=
UCIRCLE_API_URL=
UCIRCLE_API_TOKEN=
DATABASE_URL=
```

File `.env` không được commit lên GitHub.

---

# 45. Prompt — Analyzer

Input:

```text
Bạn là AI biên tập tin.

Hãy phân tích bài viết được cung cấp.

Chỉ dựa trên thông tin có trong nguồn.
Không suy đoán.
Không thêm dữ liệu.

Trả JSON gồm:

topic
relevance_score
trust_score
freshness_score
is_sensitive
publish
reason
key_facts
```

---

# 46. Prompt — Writer

```text
Bạn là AI biên tập nội dung cho UCircle.

Nhiệm vụ:
Viết một bản tóm tắt mới dựa trên các facts đã xác minh.

Yêu cầu:

- 100–200 từ.
- Không copy nguyên văn.
- Không thêm dữ liệu.
- Không bịa quote.
- Không suy đoán.
- Văn phong dễ đọc.
- Tạo 3 bullet points.
- Tạo 3–5 hashtag.

Luôn giữ:
source_name
source_url
```

---

# 47. Prompt — Image Prompt Builder

```text
Dựa trên bài viết đã xác minh, hãy tạo prompt cho AI Image Generator.

Yêu cầu:

- ảnh minh họa mới hoàn toàn
- không sao chép ảnh nguồn
- không watermark
- không text trên ảnh
- phù hợp nội dung bài
- không gây hiểu nhầm là ảnh hiện trường thật

Nếu bài nhạy cảm:
chỉ tạo ảnh biểu tượng hoặc infographic.
```

---

# 48. Prompt — Validator

```text
Kiểm tra bài viết trước khi đăng.

Reject nếu:

- không có nguồn
- không có URL
- thông tin không khớp nguồn
- có số liệu không được xác minh
- có quote không tồn tại
- bài trùng
- headline gây hiểu nhầm
- ảnh không phù hợp
- ảnh AI có nguy cơ được hiểu là ảnh sự kiện thật

Trả:

PASS
REVIEW
REJECT
```

---

# 49. Development Roadmap

## PHASE 1 — Foundation

Mục tiêu:

```text
Cài n8n
Tạo Gemini API
Tạo Source Registry
Lấy RSS
```

Kết quả:

```text
RSS → n8n → dữ liệu bài viết
```

---

## PHASE 2 — AI Analyzer

Thêm:

```text
Gemini
```

Kết quả:

```text
Article
→ Gemini
→ relevance score
→ publish / reject
```

---

## PHASE 3 — AI Writer

Kết quả:

```text
Article
→ Analyzer
→ Writer
→ UCircle Draft
```

---

## PHASE 4 — AI Image

Kết quả:

```text
Draft
→ Image Prompt
→ Gemini Image
→ Draft + Image
```

---

## PHASE 5 — Storage

Thêm:

```text
SQLite
```

Lưu:

```text
articles
drafts
images
logs
```

---

## PHASE 6 — Duplicate Engine

Thêm:

```text
URL duplicate
title similarity
semantic duplicate
```

---

## PHASE 7 — Manual Review

Workflow:

```text
AI Draft
→ Review
→ Approve
→ Publish
```

Chạy test:

```text
50–100 bài
```

---

## PHASE 8 — UCircle Integration

Ưu tiên:

```text
UCircle API
```

Nếu chưa có:

```text
Playwright Publisher Adapter
```

---

## PHASE 9 — Semi Auto

Rule:

```text
score >= 90
→ auto publish

70–89
→ review

<70
→ reject
```

---

## PHASE 10 — Multi Circle

Một engine phục vụ:

```text
Tin Tức 24h
Nét đẹp bóng đá
Đà Nẵng ăn gì
Mẹo hay mỗi ngày
...
```

---

# 50. MVP Success Criteria

MVP được coi là hoàn thành khi:

```text
□ Tự lấy được bài RSS
□ Không lấy lại bài đã xử lý
□ Gemini phân loại đúng
□ Gemini tạo bài tóm tắt
□ Có link bài gốc
□ Có tên nguồn
□ Gemini tạo được ảnh minh họa
□ Có nhãn ảnh AI
□ Draft được lưu
□ Có review
□ Đăng được lên UCircle
□ Lưu được post_id
□ Có publish log
□ Không đăng bài trùng
```

---

# 51. Quy trình phát triển đề xuất

Không xây tất cả cùng lúc.

Thứ tự chính xác:

```text
01. Cài n8n Community
02. Kết nối RSS
03. Lấy một bài thật
04. Chuẩn hóa dữ liệu
05. Lưu URL bài
06. Chặn URL trùng
07. Kết nối Gemini
08. Gemini phân tích
09. Gemini viết bài
10. Gemini tạo prompt ảnh
11. Gemini tạo ảnh
12. Content Validator
13. Lưu Draft
14. Manual Review
15. Kết nối UCircle
16. Publish một bài thử
17. Lưu log
18. Test 50–100 bài
19. Bật Semi Auto
20. Mở rộng Multi Circle
```

---

# 52. Workflow V1 cuối cùng

```text
              SCHEDULE
                  ↓
             RSS SOURCES
                  ↓
               NEW?
           ┌──────┴──────┐
           │             │
          NO            YES
           │             ↓
          STOP        NORMALIZE
                         ↓
                    URL DUPLICATE?
                   ┌─────┴─────┐
                   │           │
                  YES          NO
                   │           ↓
                  STOP      BASIC FILTER
                               ↓
                         GEMINI ANALYZER
                               ↓
                           PUBLISH?
                         ┌─────┴─────┐
                         │           │
                        NO          YES
                         │           ↓
                       REJECT     AI WRITER
                                     ↓
                              IMAGE PROMPT
                                     ↓
                                AI IMAGE
                                     ↓
                                VALIDATOR
                               ┌─────┴─────┐
                               │           │
                             FAIL         PASS
                               │           ↓
                             REVIEW      DRAFT
                                           ↓
                                     PUBLISH QUEUE
                                           ↓
                                        UCIRCLE
                                           ↓
                                      PUBLISH LOG
```

---

# 53. Tầm nhìn sau MVP

Sau khi hệ thống ổn định có thể nâng cấp:

```text
AI tự phát hiện trend
AI xếp hạng bài đáng đăng
AI tạo nhiều phiên bản caption
AI tạo thumbnail theo Circle
AI tự chọn lịch đăng
Dashboard analytics
Multi-account
Multi-Circle
Multi-language
Comment moderation
Engagement analytics
Content performance scoring
```

---

# 54. Kết luận

Kiến trúc cuối cùng được chốt:

```text
RSS / Official Source
        ↓
n8n
        ↓
Normalize
        ↓
Deduplicate
        ↓
Gemini Analyze
        ↓
Gemini Writer
        ↓
Gemini Image
        ↓
Validator
        ↓
Storage
        ↓
Publish Queue
        ↓
UCircle
        ↓
Log
```

Mục tiêu của phiên bản đầu tiên không phải tạo một hệ thống khổng lồ.

Mục tiêu là chứng minh một luồng hoàn chỉnh:

```text
1 bài báo thật
→ AI đọc
→ AI phân tích
→ AI viết bản tóm tắt
→ AI tạo ảnh minh họa mới
→ lưu Draft
→ đăng UCircle
→ lưu log thành công
```

Sau khi luồng này hoạt động ổn định, hệ thống mới được mở rộng lên nhiều nguồn, nhiều bài và nhiều Circle.

---

# NEXT STEP

Bước phát triển tiếp theo:

```text
STEP 01
Cài n8n Community trên Windows

STEP 02
Tạo workflow:

Schedule
→ RSS
→ lấy 1 bài
→ xem JSON đầu ra
```

Chỉ sau khi bước này chạy thành công mới bắt đầu tích hợp Gemini.
