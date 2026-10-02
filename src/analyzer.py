"""
src/analyzer.py
Module 4 — Gemini AI Analyzer (dung google.genai SDK moi)
Phan tich bai viet va cham diem muc do phu hop de dang len Circle
"""

import json
import html
import re
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from .logger import get_logger
from .gemini_client import generate_json

logger = get_logger("analyzer")

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "analyzer.md"


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value or "")))
    return " ".join(text.split())


def _fallback_analysis(article: dict) -> dict:
    """Phân tích bảo thủ từ dữ liệu nguồn khi Gemini hết hạn mức hoặc quá tải."""
    title = _clean_text(article.get("original_title"))
    description = _clean_text(article.get("original_description"))
    category = _clean_text(article.get("category") or "khác").casefold()
    combined = f"{title} {description}".casefold()
    topic_rules = {
        "thể thao": ("bóng đá", "cầu thủ", "trận đấu", "đội tuyển"),
        "công nghệ": ("công nghệ", "phần mềm", "điện thoại", "máy tính", "ai "),
        "kinh tế": ("kinh tế", "doanh nghiệp", "lãi suất", "thị trường", "ngân hàng"),
        "y tế": ("y tế", "bệnh viện", "sức khỏe", "bác sĩ"),
        "giáo dục": ("giáo dục", "học sinh", "sinh viên", "trường học"),
        "thế giới": ("quốc tế", "thế giới", "châu âu", "châu á"),
        "đời sống": ("mẹo", "hướng dẫn", "gia dụng", "nấu ăn", "ẩm thực"),
    }
    topic = next(
        (name for name, markers in topic_rules.items() if any(marker in combined for marker in markers)),
        category if category in {"thời sự", "kinh tế", "công nghệ", "y tế", "giáo dục", "thế giới", "xã hội", "đời sống", "thể thao"} else "khác",
    )
    sentence_parts = [
        part.strip(" .") for part in re.split(r"(?<=[.!?])\s+|\s*[;•]\s*", description)
        if len(part.strip()) >= 20
    ]
    key_facts = sentence_parts[:3]
    if not key_facts and title:
        key_facts = [title]
    if len(key_facts) == 1:
        key_facts.append(f"Nguồn thông tin: {_clean_text(article.get('source_name')) or 'bài gốc'}")

    sensitive_markers = ("tử vong", "thiệt mạng", "tai nạn", "án mạng", "bạo lực", "xâm hại", "tự tử")
    is_sensitive = any(marker in combined for marker in sensitive_markers)
    trust_score = max(50, min(90, int(article.get("trust_level") or 75)))
    content_quality = 78 if len(description.split()) >= 30 else 68
    overall_score = round((78 + trust_score + 75 + content_quality) / 4)
    source_ready = bool(title and description and article.get("source_url"))
    safe_topic_tag = re.sub(r"[^\wÀ-ỹ]+", "", topic.title()) or "TinTuc"
    return {
        "topic": topic,
        "relevance_score": 78,
        "trust_score": trust_score,
        "freshness_score": 75,
        "content_quality_score": content_quality,
        "overall_score": overall_score,
        "is_sensitive": is_sensitive,
        "sensitivity_level": "medium" if is_sensitive else "low",
        "publish": source_ready,
        "reason": "Gemini tạm không khả dụng; hệ thống chỉ phân tích dữ liệu có trong tiêu đề, mô tả và nguồn. Bài bắt buộc duyệt thủ công.",
        "key_facts": key_facts,
        "suggested_hashtags": [f"#{safe_topic_tag}", "#TinChonLoc", "#NguonRoRang"],
        "analysis_mode": "local_fallback",
    }


def _load_prompt_template() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _publish_threshold(circle: dict | None) -> int:
    circle = circle or {}
    configured = circle.get("analysis_publish_threshold")
    try:
        configured_value = int(configured or 0)
    except (TypeError, ValueError):
        configured_value = 0
    if configured_value:
        return max(55, min(95, configured_value))
    circle_name = str(
        circle.get("display_name") or circle.get("circle_name") or "Tin Tức 24h"
    ).strip()
    circle_id = str(circle.get("local_circle_id") or circle.get("circle_id") or "").casefold()
    return 85 if "tintuc24h" in circle_id or "tin tức 24h" in circle_name.casefold() else 70


def _build_prompt(article: dict, circle: dict | None = None) -> str:
    circle = circle or {}
    circle_name = str(
        circle.get("display_name") or circle.get("circle_name") or "Tin Tức 24h"
    ).strip()
    # Ngưỡng mặc định vẫn cao; từng profile có thể cấu hình ngoài mã nguồn.
    publish_threshold = _publish_threshold(circle)
    circle_content_prompt = str(circle.get("content_prompt") or "").strip()
    selection_prompt = str(circle.get("selection_prompt") or "").strip()
    if not circle_content_prompt:
        circle_content_prompt = (
            "Ưu tiên tin mới, nguồn rõ ràng, đủ dữ kiện để tóm tắt; loại bỏ tin đồn, "
            "nội dung sai lệch, quảng cáo trá hình và nội dung không thể kiểm chứng."
        )
    template = _load_prompt_template()
    base_prompt = (
        template
        .replace("{{circle_name}}", circle_name)
        .replace("{{circle_content_prompt}}", circle_content_prompt)
        .replace("{{publish_threshold}}", str(publish_threshold))
        .replace("{{title}}", article.get("original_title", ""))
        .replace("{{description}}", article.get("original_description", "")[:3500])
        .replace("{{source_name}}", article.get("source_name", ""))
        .replace("{{source_url}}", article.get("source_url", ""))
        .replace("{{published_at}}", article.get("published_at", ""))
        .replace("{{category}}", article.get("category", "news"))
        .replace("{{sensitivity_level}}", article.get("_sensitivity_level", "low"))
    )
    return (
        f"{base_prompt}\n\n"
        + (f"QUY TẮC LỌC CHUNG BẮT BUỘC:\n{selection_prompt}\n\n" if selection_prompt else "")
        + "THÔNG TIN NGUỒN ĐA PHƯƠNG TIỆN:\n"
        f"- Nền tảng: {article.get('platform', 'news')}\n"
        f"- Loại nội dung: {article.get('media_type', 'article')}\n"
        f"- Giấy phép: {article.get('license_type', 'unknown')}\n"
        f"- Quyền sử dụng: {article.get('rights_status', 'LINK_ONLY')}\n"
        "Không suy đoán rằng nội dung không có bản quyền. Nếu quyền là LINK_ONLY, "
        "chỉ được tóm tắt và dẫn liên kết nguồn; không đề xuất tải hoặc đăng lại media."
    )


def analyze_article(
    article: dict,
    max_retries: int = 3,
    circle: dict | None = None,
) -> dict | None:
    """
    Phan tich bai viet bang Gemini AI.
    """
    title = article.get("original_title", "")[:60]
    logger.info(f"Analyzing: {title}...")

    prompt = _build_prompt(article, circle=circle)

    config_path = Path(__file__).parent.parent / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        ai_config = json.load(f).get("ai", {})

    for attempt in range(1, max_retries + 1):
        try:
            result = generate_json(
                prompt,
                temperature=float(ai_config.get("temperature", 0.3)),
                purpose="phân tích bài",
                preferred_model=ai_config.get("model_text"),
                media_url=(
                    article.get("source_url")
                    if article.get("platform") == "youtube"
                    else None
                ),
            )

            required = ["topic", "overall_score", "publish", "reason"]
            if not all(k in result for k in required):
                logger.warning(f"JSON thieu truong (attempt {attempt})")
                continue

            for score_field in ["relevance_score", "trust_score", "freshness_score",
                                 "content_quality_score", "overall_score"]:
                if score_field in result:
                    result[score_field] = int(result.get(score_field, 0))

            threshold = _publish_threshold(circle)
            if (
                circle
                and circle.get("analysis_allow_threshold_override", False)
                and not result.get("publish", False)
                and int(result.get("overall_score", 0) or 0) >= threshold
                and not result.get("is_sensitive", False)
            ):
                reason_text = str(result.get("reason") or "").casefold()
                hard_blockers = (
                    "nguy hiểm", "không kiểm chứng", "sai lệch", "vi phạm",
                    "chữa bệnh", "cá cược", "quảng cáo trá hình", "bịa đặt",
                )
                if not any(marker in reason_text for marker in hard_blockers):
                    result["publish"] = True
                    result["reason"] = (
                        f"{str(result.get('reason') or '').strip()} "
                        f"Bài đạt ngưỡng riêng {threshold}/100 của Circle và đã vượt bộ lọc chủ đề chặt."
                    ).strip()

            logger.info(
                f"  Score: {result.get('overall_score', 0)}/100 | "
                f"Publish: {result.get('publish')} | "
                f"Topic: {result.get('topic')}"
            )
            return result

        except Exception as e:
            logger.error(f"Gemini Analyzer loi (attempt {attempt}): {e}")
            error_text = str(e).casefold()
            # Không gọi lặp lại Gemini khi dịch vụ đã báo quá tải/hết hạn mức
            # hoặc hết thời gian chờ. Với gói miễn phí, thử tiếp chỉ làm một lần
            # quét kéo dài thêm vài phút mà hiếm khi cải thiện kết quả.
            if any(marker in error_text for marker in (
                "429",
                "resource_exhausted",
                "503",
                "504",
                "unavailable",
                "timeout",
                "timed out",
                "deadline_exceeded",
                "deadline expired",
            )):
                break
            if attempt < max_retries:
                time.sleep(5 * attempt)

    fallback = _fallback_analysis(article)
    logger.warning(
        f"Gemini chưa sẵn sàng; dùng Analyzer dự phòng cho '{title}' "
        f"(score={fallback['overall_score']})."
    )
    return fallback
