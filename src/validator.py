"""
src/validator.py
Module 9 — Content Validator
Kiểm tra tính hợp lệ và tuân thủ nguyên tắc trước khi xuất bản hoặc lưu vào Publish Queue
"""

import json
import re
from .logger import get_logger

logger = get_logger("validator")

def validate_post(post_data: dict, original_article: dict, image_meta: dict = None) -> dict:
    """
    Kiểm tra bài viết theo checklist 12 tiêu chí của quy chuẩn kiến trúc.
    
    Returns:
        {
            "status": "READY" | "REVIEW" | "REJECTED",
            "score": int,
            "reasons": list[str],
            "passed": bool
        }
    """
    reasons = []
    score = 100

    ai_title = post_data.get("ai_title", "").strip()
    ai_summary = post_data.get("ai_summary", "").strip()
    source_url = (post_data.get("source_url") or original_article.get("source_url", "")).strip()
    source_name = (post_data.get("source_name") or original_article.get("source_name", "")).strip()
    bullet_points = post_data.get("bullet_points", [])
    hashtags = post_data.get("hashtags", [])

    # 1. Kiểm tra URL nguồn
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        reasons.append("Thiếu source_url hoặc URL không hợp lệ")
        score -= 40

    # 2. Kiểm tra tên nguồn
    if not source_name:
        reasons.append("Thiếu tên nguồn bài viết")
        score -= 30

    # 3. Kiểm tra độ dài tiêu đề
    if len(ai_title) < 10:
        reasons.append("Tiêu đề quá ngắn (< 10 ký tự)")
        score -= 25
    elif len(ai_title) > 200:
        reasons.append("Tiêu đề quá dài (> 200 ký tự)")
        score -= 10

    # 4. Kiểm tra độ dài nội dung tóm tắt
    word_count = len(ai_summary.split())
    if word_count < 40:
        reasons.append(f"Tóm tắt quá ngắn ({word_count} từ, tối thiểu 40 từ)")
        score -= 25
    elif word_count > 400:
        reasons.append(f"Tóm tắt quá dài ({word_count} từ, tối đa 400 từ)")
        score -= 10

    # 5. Kiểm tra điểm chính (bullet points)
    if not bullet_points or len(bullet_points) < 2:
        reasons.append("Thiếu điểm chính (cần tối thiểu 2 bullet points)")
        score -= 15

    # 6. Kiểm tra hashtags
    if len(hashtags) != 5:
        reasons.append(f"Cần đúng 5 hashtag liên quan (hiện có {len(hashtags)})")
        score -= 10

    # 7. Ảnh là điều kiện bắt buộc: không có ảnh thì tuyệt đối không READY.
    has_image = False
    if image_meta:
        label = image_meta.get("image_label", "")
        has_image = bool(image_meta.get("file_path")) and image_meta.get("status") == "PASS"
        is_ai_generated = bool(image_meta.get("is_ai_generated", True))
        if not has_image:
            reasons.append("Thiếu ảnh hợp lệ; bài không được phép đăng")
            score -= 50
        elif is_ai_generated and "Ảnh minh họa AI" not in label:
            reasons.append("Ảnh AI chưa có nhãn minh họa rõ ràng")
            score -= 15
        elif not is_ai_generated and "Ảnh từ bài gốc" not in label:
            reasons.append("Ảnh nguồn chưa có ghi chú xuất xứ rõ ràng")
            score -= 15

    else:
        reasons.append("Thiếu ảnh hợp lệ; bài không được phép đăng")
        score -= 50

    # 8. Phân định trạng thái
    if not has_image:
        final_status = "REJECTED"
        passed = False
    elif score >= 85 and not any("Thiếu source" in r for r in reasons):
        final_status = "READY"
        passed = True
    elif score >= 65:
        final_status = "REVIEW"
        passed = False
    else:
        final_status = "REJECTED"
        passed = False

    logger.info(f"🔎 Validation Post: score={score}, status={final_status}, reasons={reasons}")

    return {
        "status": final_status,
        "score": max(0, score),
        "reasons": reasons,
        "passed": passed
    }
