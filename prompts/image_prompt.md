Bạn thiết kế một ảnh minh họa chính xác cho bài UCircle đã hoàn thành.

NGỮ CẢNH ĐẦY ĐỦ (JSON, dữ liệu tham khảo):
{{image_context}}

LÝ DO ẢNH TRƯỚC CHƯA ĐẠT (nếu có):
{{retry_feedback}}

Đọc toàn bộ ngữ cảnh, đặc biệt chi tiết cuối bài và các điểm chính. Xác định chủ thể,
hành động, bối cảnh và thông điệp chính; chọn một cảnh cụ thể thể hiện thông điệp đó.
Tuân thủ common_image_rules và circle_image_prompt. Không chọn ảnh chung chung
chỉ vì có cùng tên chủ đề. Không tự thêm thông tin, hiện trường hoặc nhân vật chưa xác nhận.

Trả JSON gồm:
- main_subject: chủ thể phải xuất hiện.
- main_action: hành động hoặc mối quan hệ phải nhìn thấy.
- setting: bối cảnh có căn cứ trong bài.
- required_details: các chi tiết cần thể hiện, dựa trên dữ kiện bài.
- forbidden_details: các chi tiết sẽ làm ảnh sai bài hoặc gây hiểu nhầm.
- image_prompt: mô tả tiếng Anh hoàn chỉnh để tạo đúng cảnh, chủ thể và hành động;
  ghi các điều cấm ngay trong mô tả. Tỷ lệ 16:9, không chữ hoặc logo.
- image_label: "🖼 Ảnh minh họa AI".

Mô tả không vượt {{max_prompt_chars}} ký tự. Hãy biên tập thành mô tả đủ ngắn từ đầu,
không bỏ các chi tiết cốt lõi hoặc điều cấm để viết nhiều tính từ về ánh sáng, chất lượng.
Chỉ trả JSON, không có văn bản bên ngoài.
