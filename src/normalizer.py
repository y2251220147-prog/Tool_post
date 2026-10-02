"""
src/normalizer.py
Module 2 — Data Normalizer
Chuẩn hóa bài viết từ nhiều nguồn RSS về cùng schema thống nhất
"""

import re
import json
from datetime import datetime
from .logger import get_logger

logger = get_logger("normalizer")

# Từ khóa cơ bản — lọc Tier 1 trước khi gửi AI
RELEVANT_KEYWORDS = [
    # Thời sự
    "chính phủ", "quốc hội", "thủ tướng", "bộ trưởng", "chính sách",
    "luật", "nghị định", "quyết định", "phê duyệt", "ban hành",
    # Kinh tế
    "kinh tế", "tăng trưởng", "gdp", "lạm phát", "xuất khẩu", "nhập khẩu",
    "doanh nghiệp", "ngân hàng", "tín dụng", "đầu tư", "thị trường",
    "chứng khoán", "vàng", "ngoại tệ", "tỷ giá",
    # Xã hội
    "giáo dục", "y tế", "sức khỏe", "bệnh viện", "trường học",
    "an toàn", "tai nạn", "thiên tai", "lũ lụt", "bão",
    # Công nghệ
    "công nghệ", "ai", "trí tuệ nhân tạo", "digital", "internet",
    "smartphone", "chip", "phần mềm", "ứng dụng",
    # Thế giới
    "thế giới", "quốc tế", "liên hợp quốc", "mỹ", "trung quốc",
    "nga", "châu âu", "asean", "ngoại giao", "thương mại",
]

SENSITIVE_KEYWORDS = [
    "chết", "tử vong", "thương vong", "xung đột", "chiến tranh",
    "bom", "khủng bố", "bắt cóc", "giết người", "cưỡng hiếp",
    "tai nạn giao thông", "cháy nổ", "sập", "lật xe", "đắm thuyền",
]


def clean_html(text: str) -> str:
    """Xóa HTML tags và làm sạch text."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_sensitivity(title: str, description: str) -> dict:
    """Phát hiện nội dung nhạy cảm."""
    text = f"{title} {description}".lower()
    sensitive_found = [kw for kw in SENSITIVE_KEYWORDS if kw in text]
    is_sensitive = len(sensitive_found) > 0
    return {
        "is_sensitive": is_sensitive,
        "sensitive_keywords": sensitive_found,
        "sensitivity_level": "high" if len(sensitive_found) >= 2 else ("medium" if is_sensitive else "low"),
    }


def basic_keyword_filter(title: str, description: str) -> bool:
    """
    Tier 1 filter — lọc bằng code, không cần AI.
    True = bài có thể liên quan, False = bỏ qua.
    """
    text = f"{title} {description}".lower()
    # Nếu không có keyword nào → bỏ qua
    return any(kw in text for kw in RELEVANT_KEYWORDS)


def normalize_article(raw: dict) -> dict | None:
    """
    Normalize một bài viết thô thành schema chuẩn.
    
    Returns:
        Dict chuẩn hóa hoặc None nếu bài không đạt yêu cầu tối thiểu
    """
    title = clean_html(raw.get("original_title", "")).strip()
    description = clean_html(raw.get("original_description", "")).strip()
    source_url = raw.get("source_url", "").strip()

    # Validation cơ bản
    if not title or len(title) < 10:
        logger.debug(f"⏭ Bỏ qua — tiêu đề quá ngắn: {title[:50]}")
        return None

    if not source_url or not source_url.startswith("http"):
        logger.debug(f"⏭ Bỏ qua — URL không hợp lệ: {source_url[:50]}")
        return None

    # Chuẩn hóa published_at
    published_at = raw.get("published_at", "")
    if not published_at:
        published_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Detect sensitivity
    sensitivity = detect_sensitivity(title, description)

    normalized = {
        # DB fields
        "source_id": raw.get("source_id", ""),
        "source_name": raw.get("source_name", ""),
        "source_url": source_url,
        "original_title": title,
        "original_description": description,
        "author": raw.get("author", ""),
        "published_at": published_at,
        "category": raw.get("category", "news"),
        "content_hash": raw.get("content_hash", ""),
        "source_image_url": raw.get("source_image_url", ""),
        "platform": raw.get("platform", "news"),
        "media_type": raw.get("media_type", "article"),
        "external_id": raw.get("external_id", ""),
        "embed_url": raw.get("embed_url", ""),
        "thumbnail_url": raw.get("thumbnail_url", ""),
        "license_type": raw.get("license_type", "unknown"),
        "rights_status": raw.get("rights_status", "LINK_ONLY"),
        "status": "NEW",
        # Extra metadata (dùng trong pipeline)
        "_trust_level": raw.get("_trust_level", 80),
        "_requires_attribution": raw.get("_requires_attribution", True),
        "_image_policy": raw.get("_image_policy", "DO_NOT_COPY"),
        "_is_sensitive": sensitivity["is_sensitive"],
        "_sensitivity_level": sensitivity["sensitivity_level"],
        "_sensitive_keywords": sensitivity["sensitive_keywords"],
        "_hot_score": raw.get("_hot_score", 0),
        # Tier 1 filter result
        "_passes_keyword_filter": basic_keyword_filter(title, description),
    }

    return normalized


def normalize_batch(raw_articles: list[dict]) -> tuple[list[dict], int]:
    """
    Normalize một batch bài viết.
    
    Returns:
        (danh sách đã normalize, số bài bị loại)
    """
    normalized = []
    skipped = 0

    for raw in raw_articles:
        result = normalize_article(raw)
        if result:
            normalized.append(result)
        else:
            skipped += 1

    logger.info(f"📝 Normalize: {len(normalized)} bài hợp lệ, {skipped} bị loại")
    return normalized, skipped
