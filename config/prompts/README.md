# Cấu hình prompt ngoài mã nguồn

Thư mục này chứa nội dung prompt dùng chung và prompt riêng theo chủ đề. Có thể sửa các file Markdown rồi tải lại Dashboard; không cần sửa mã Python.

- `common.md`: quy tắc chung về kiểm chứng, chống trùng, bản quyền, cấu trúc bài và 5 hashtag.
- `selection.md`: prompt ngắn cho bước quét/phân loại nhằm tiết kiệm hạn mức Gemini.
- Các file còn lại: yêu cầu nội dung riêng cho từng hồ sơ Circle.
- `../prompt_profiles.json`: ánh xạ `profile_id`, tên thay thế, danh mục, từ khóa tìm kiếm và bộ lọc dự phòng tới từng file prompt.

Khi đổi tài khoản UCircle và bấm **Quét Circle**, hệ thống tự ghép hồ sơ theo tên hoặc danh mục. Circle chưa nhận diện được sẽ dùng `generic.md`. Trên Dashboard có thể chọn lại hồ sơ cho từng Circle.

Nếu sửa trực tiếp nội dung prompt trên Dashboard, hệ thống chỉ lưu phần đó thành bản ghi đè riêng của Circle. Muốn Circle tiếp tục nhận cập nhật từ file Markdown, chọn lại hồ sơ tương ứng; ô prompt sẽ được nạp lại từ cấu hình ngoài, sau đó bấm **Lưu cấu hình**.

Hai biến có thể dùng trong file prompt:

- `{{circle_name}}`: tên Circle hiện tại.
- `{{category_name}}`: danh mục UCircle hiện tại.

Không đưa API key, mật khẩu, token hoặc thông tin đăng nhập vào các file prompt.

## Điều chỉnh tìm kiếm và bộ lọc

Trong từng hồ sơ của `../prompt_profiles.json`:

- `media_search_variants`: các truy vấn ngắn, đúng chủ đề; hệ thống luân phiên ba truy vấn báo và một truy vấn YouTube mỗi lượt.
- `media_max_age_days`: giới hạn tuổi nguồn (tối đa 90 ngày). Bài tham khảo không được viết như tin mới hôm nay; giá và giờ mở cửa phải được kiểm tra lại.
- `fallback_filter.all_groups`: ứng viên phải khớp ít nhất một cụm trong mỗi nhóm; không cần khớp mọi cụm của nhóm.
- `fallback_filter.priority_phrases`: tăng ưu tiên cho tiêu đề có cụm thể hiện trọng tâm như món ăn hoặc pha bóng.
- `fallback_filter.title_exclude`: loại ngay khi cụm xuất hiện trong tiêu đề; không loại một bài chỉ vì phần thân nhắc đến cụm đó để đối chiếu.
- `fallback_filter.exclude`: loại khi cụm xuất hiện trong tiêu đề, mô tả hoặc chuyên mục.
- `fallback_filter.deprioritize_sources`: giảm ưu tiên nguồn có mô tả dưới 90 từ, chẳng hạn trang tổng hợp video thiếu diễn biến cụ thể; không cấm hoàn toàn nguồn đó.

Bộ lọc này chỉ chọn ứng viên. Gemini vẫn thẩm định nội dung thực tế; việc vượt từ khóa không đảm bảo bài được đăng. Trang video chỉ có tiêu đề không đủ để dựng lại tình huống. Khi tất cả ứng viên bị loại, thông báo giữ lý do gần nhất thay vì chỉ nói chung chung rằng không có tin.

## Cấu hình ảnh theo nội dung bài viết

- `image_common.md`: hướng dẫn ảnh dùng chung, được nạp lại mỗi lần xử lý. Việc sửa file này cũng làm ảnh đã kiểm tra trước đó cần được kiểm tra lại trước lần đăng tiếp theo.
- `../prompt_profiles.json`, trường `image_prompt` của từng hồ sơ: hướng dẫn ảnh theo chủ đề. Có thể dùng `image_prompt_file` để trỏ tới một file Markdown riêng, giống cách cấu hình prompt nội dung.
- `image_strategy`: đặt `source_only` để chỉ nhận ảnh gốc đã kiểm tra, không gọi tạo ảnh AI; nếu thiếu ảnh đạt yêu cầu thì bài ở trạng thái Chờ ảnh. Có thể đặt `image_strategy_override` trong cấu hình riêng Circle nếu cần chính sách khác.
- Dashboard → cấu hình của từng Circle → **Hướng dẫn ảnh riêng cho Circle**: chỉnh đối tượng, thao tác và bối cảnh cần thể hiện. Bấm **Lưu cấu hình**; bản ghi đè được lưu theo Circle, không cấu hình chết theo tài khoản.
- `../../prompts/image_prompt.md`: mẫu yêu cầu Gemini thiết kế cảnh. Ngữ cảnh bao gồm toàn bộ bài mới viết, tiêu đề, tóm tắt, điểm chính, hashtag, nguồn, sự kiện đã phân tích và hướng dẫn Circle; không cắt còn 900 ký tự.
- `../../prompts/image_validation.md`: yêu cầu Gemini nhìn file ảnh thật và đối chiếu với toàn bộ bài, không chỉ kiểm tra file tải được.

Ở phần tài khoản trên Dashboard, mở **Cấu hình dịch vụ ảnh** để chọn dịch vụ và lưu. Mặc định chung ưu tiên ảnh nguồn; nếu không đạt thì mới dùng dịch vụ tạo ảnh theo cấu hình chung. Hồ sơ `da_nang_an_gi` đặt `image_strategy` là `source_only`, vì vậy không sinh ảnh AI và sẽ chờ ảnh nếu ảnh nguồn không khớp hoặc chưa kiểm tra đạt. Ảnh trên mạng có thể thuộc bản quyền; cần bảo đảm quyền sử dụng và giữ nguồn ảnh khi đăng.

Tùy chọn **Gemini trực tiếp sinh ảnh** dùng model ảnh riêng. Chức năng này chỉ gọi API sinh ảnh khi đã chọn Gemini và tích bật; mặc định tắt để tránh phát sinh chi phí ngoài ý muốn. Model mặc định `gemini-3.1-flash-image` không có gói API miễn phí theo [bảng giá Google](https://ai.google.dev/gemini-api/docs/pricing). Không tự chuyển sang Gemini tính phí khi Pollinations lỗi.

Mọi ảnh, kể cả ảnh nguồn, phải được kiểm tra chủ thể, thao tác, bối cảnh, chi tiết sai lệch và lỗi hình ảnh; mặc định yêu cầu ít nhất 85/100. Kiểm tra phù hợp nội dung không đồng nghĩa xác nhận quyền sử dụng ảnh; vẫn cần tuân thủ quyền của nguồn.

Ảnh chưa đạt, Gemini hết hạn mức hoặc dịch vụ lỗi: bài được giữ **Chờ ảnh**, không được đăng. Bấm **Kiểm tra / tiếp tục ảnh** để xử lý phần còn thiếu, không quét và viết lại nội dung. Nếu đã có ảnh thì chỉ kiểm tra lại; nếu đã có prompt nhưng dịch vụ chưa trả ảnh thì sử dụng lại prompt đã lưu. Chế độ tự động cũng ưu tiên hoàn tất bài Chờ ảnh trước khi chọn nguồn mới.

Bấm **Tạo ảnh AI mới** nếu muốn bỏ qua ảnh hiện tại và tạo lại. Mỗi lần xử lý tạo tối đa hai ảnh AI để hạn chế tiêu hao; nếu vẫn không đạt thì tiếp tục Chờ ảnh. Lệnh dừng/tạm dừng được kiểm tra giữa các bước; yêu cầu mạng đang chạy có thể cần kết thúc hoặc hết thời gian chờ trước khi dừng.

Kết quả, dịch vụ, model, prompt, ngữ cảnh đã gửi và lịch sử kiểm tra được lưu trong `media_assets`; không xóa dữ liệu cũ. Chỉ ảnh của lần xử lý hiện tại được dùng để đăng. Các bài cũ chưa có kiểm tra ảnh theo cơ chế mới phải bấm kiểm tra trước khi đăng. Sửa bài hoặc prompt Circle sẽ làm mất hiệu lực kết quả cũ, yêu cầu kiểm tra lại.

`pipeline.py`, `publish_pipeline.py` và Dashboard cùng dùng `src/image_workflow.py` để khóa công việc ảnh và xác định trạng thái. Công cụ `scripts/backfill_images.py` chỉ bổ sung ảnh nguồn, vẫn phải kiểm tra lại ảnh thật; không tạo AI, không đăng bài và không tự gắn PASS chỉ vì tải ảnh thành công.

## Các file của đợt nâng cấp ảnh 29/09/2026

- Ngữ cảnh, tạo ảnh và thẩm định: `src/image_context.py`, `src/image_generator.py`, `src/image_workflow.py`, `src/gemini_client.py`.
- Dữ liệu và chặn đăng sai ảnh: `src/storage.py`, `src/publisher.py`.
- Hồ sơ Circle: `src/prompt_profiles.py`, `src/ucircle_account.py`, `config/prompt_profiles.json`.
- Cấu hình và mẫu yêu cầu: `config/automation.json`, `config/prompts/image_common.md`, `prompts/image_prompt.md`, `prompts/image_validation.md`, tài liệu `config/prompts/README.md` này.
- Dashboard: `review_ui/app.py`, `review_ui/templates/dashboard.html`, `review_ui/static/css/style.css`.
- Các công cụ chạy: `pipeline.py`, `publish_pipeline.py`, `scripts/backfill_images.py`.
- Kiểm tra: `tests/test_image_workflows.py`, `tests/test_core_workflows.py`.

Kiểm tra tự động: `python -m unittest discover -s tests` (104 trường hợp) và `python -m compileall -q src review_ui/app.py pipeline.py publish_pipeline.py scripts/backfill_images.py tests`. Bộ kiểm tra dùng DB tạm và dịch vụ giả lập; không đăng Circle. Đã kiểm tra riêng bằng API thật: một ảnh nguồn đạt 95/100, một ảnh AI qua luồng phản hồi/thử lại đạt 85/100. Ảnh thử sinh mới chỉ lưu thư mục tạm. Chưa kiểm tra sinh ảnh Gemini tính phí hoặc đăng bài thật trong đợt nâng cấp này. Điểm kiểm tra không phải cam kết mọi ảnh sinh ra sẽ luôn chính xác.

Khóa xử lý ảnh ghi PID của worker. Khi máy chủ khởi động lại, bài có worker đã kết thúc được mở khóa để tiếp tục; không mở khóa nhầm bài vẫn đang xử lý hoặc tạm dừng trong tiến trình khác.
