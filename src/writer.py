"""
src/writer.py
Module 6 — Gemini AI Writer (dung google.genai SDK moi)
Viet bai tom tat moi dua tren bai bao goc va ket qua phan tich
"""

import json
import html
import re
import time
from pathlib import Path
from dotenv import load_dotenv
from .logger import get_logger
from .gemini_client import generate_json
from .ucircle_account import get_circle_config

load_dotenv()
logger = get_logger("writer")

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "writer.md"


def _load_prompt_template() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _get_circle_config(circle_id: str) -> dict:
    """Lấy config của một circle."""
    return get_circle_config(circle_id)


def _format_persona(circle: dict) -> str:
    """Format persona thành text dễ đọc."""
    persona = circle.get("persona", {})
    lines = []
    if persona.get("tone"):
        lines.append(f"- Tone: {persona['tone']}")
    if persona.get("style"):
        lines.append(f"- Style: {persona['style']}")
    if persona.get("emoji_level"):
        lines.append(f"- Emoji: {persona['emoji_level']}")
    if persona.get("forbidden"):
        lines.append(f"- Cấm: {', '.join(persona['forbidden'])}")
    return "\n".join(lines) if lines else "Trung lập, ngắn gọn, rõ ràng"


def _build_prompt(article: dict, analysis: dict, circle: dict) -> str:
    """Build writer prompt."""
    template = _load_prompt_template()
    key_facts_str = "\n".join(f"- {f}" for f in analysis.get("key_facts", []))
    circle_content_prompt = (circle.get("content_prompt") or "").strip()
    common_prompt = (circle.get("common_prompt") or "").strip()

    base_prompt = (
        template
        .replace("{{circle_name}}", circle.get("circle_name", "Tin Tức 24h"))
        .replace("{{original_title}}", article.get("original_title", ""))
        .replace("{{original_description}}", article.get("original_description", "")[:3500])
        .replace("{{source_name}}", article.get("source_name", ""))
        .replace("{{source_url}}", article.get("source_url", ""))
        .replace("{{topic}}", analysis.get("topic", ""))
        .replace("{{overall_score}}", str(analysis.get("overall_score", 0)))
        .replace("{{key_facts}}", key_facts_str or "Xem bài gốc")
        .replace("{{is_sensitive}}", str(analysis.get("is_sensitive", False)))
        .replace("{{circle_persona}}", _format_persona(circle))
    )
    if not circle_content_prompt:
        circle_instruction = ""
    else:
        circle_instruction = (
        "YÊU CẦU RIÊNG BẮT BUỘC CỦA CIRCLE NÀY:\n"
        f"{circle_content_prompt}\n"
        "Nếu bài gốc không phù hợp yêu cầu riêng, không được bẻ nội dung sang chủ đề khác."
        )
    rights_instruction = (
        "\n\nQUY TẮC NGUỒN MEDIA:\n"
        f"- Nền tảng: {article.get('platform', 'news')}\n"
        f"- Loại: {article.get('media_type', 'article')}\n"
        f"- Giấy phép: {article.get('license_type', 'unknown')}\n"
        f"- Trạng thái quyền: {article.get('rights_status', 'LINK_ONLY')}\n"
        "Luôn ghi nguồn và URL. Không tuyên bố sở hữu video/ảnh của nguồn. "
        "Nếu trạng thái là LINK_ONLY, chỉ viết bài dẫn nguồn, không yêu cầu sao chép media."
    )
    common_instruction = (
        f"QUY TẮC BIÊN TẬP CHUNG BẮT BUỘC:\n{common_prompt}\n\n"
        if common_prompt else ""
    )
    return f"{base_prompt}\n\n{common_instruction}{circle_instruction}{rights_instruction}"


def _build_full_content(result: dict, source_url: str, source_name: str) -> str:
    """Dựng cấu trúc bài đăng thống nhất, không phụ thuộc định dạng AI trả về."""
    ai_title = result.get("ai_title", "")
    ai_summary = result.get("ai_summary", "")
    bullets = result.get("bullet_points", [])
    hashtags = result.get("hashtags", [])

    bullet_text = "\n".join(f"• {b}" for b in bullets)
    hashtag_text = " ".join(hashtags)

    return (
        f"🔥 {ai_title}\n\n"
        f"{ai_summary}\n\n"
        f"📌 Điểm chính:\n{bullet_text}\n\n"
        f"🔗 Xem chi tiết:\n{source_url}\n\n"
        f"📰 Nguồn: {source_name}\n"
        f"🖼 Ảnh từ bài gốc — Nguồn: {source_name}\n\n"
        f"{hashtag_text}"
    )


def _hashtag_from_text(value: str) -> str:
    parts = re.findall(r"[\wÀ-ỹ]+", str(value or "").replace("#", ""), flags=re.UNICODE)
    cleaned = "".join(part[:1].upper() + part[1:] for part in parts)
    return f"#{cleaned}" if len(cleaned) >= 2 else ""


def _normalize_hashtags(values, article: dict, analysis: dict, circle: dict) -> list[str]:
    """Luôn trả đúng 5 hashtag, ưu tiên chủ đề cụ thể của bài và Circle."""
    if isinstance(values, str):
        values = values.split()
    values = values if isinstance(values, list) else []
    blocked = {"#khac", "#khác", "#nguonrorang", "#tinchonloc", "#baivietmoi", "#tintuc"}

    title = re.sub(r"[^\wÀ-ỹ]+", " ", str(article.get("original_title") or ""))
    stopwords = {
        "của", "và", "với", "trong", "tại", "cho", "về", "những", "một", "các",
        "được", "đang", "sẽ", "sau", "trước", "theo", "từ", "khi", "này", "nhất",
    }
    title_words = [word for word in title.split() if len(word) >= 3 and word.casefold() not in stopwords]
    title_tags = []
    if title_words:
        title_tags.append(_hashtag_from_text(" ".join(title_words[:4])))
    if len(title_words) >= 5:
        title_tags.append(_hashtag_from_text(" ".join(title_words[4:7])))

    candidates = [
        *values,
        *title_tags,
        *(circle.get("hashtag_seeds") or []),
        _hashtag_from_text(analysis.get("topic")),
        _hashtag_from_text(circle.get("display_name") or circle.get("circle_name")),
        *(analysis.get("suggested_hashtags") or []),
        _hashtag_from_text(article.get("source_name")),
    ]
    result = []
    seen = set()
    for candidate in candidates:
        tag = _hashtag_from_text(candidate)
        key = tag.casefold()
        if not tag or key in blocked or key in seen:
            continue
        seen.add(key)
        result.append(tag)
        if len(result) == 5:
            return result

    for fallback in ("#ChiaSeHuuIch", "#ThongTinMoi", "#CongDongUCircle", "#CapNhatMoi", "#NoiDungHay"):
        key = fallback.casefold()
        if key not in seen:
            result.append(fallback)
            seen.add(key)
        if len(result) == 5:
            break
    return result[:5]


def _normalize_bullet_points(values, article: dict, analysis: dict) -> list[str]:
    """Giữ 3-5 điểm chính có nguồn, không để mục trống hoặc lặp."""
    if isinstance(values, str):
        values = [values]
    candidates = list(values or [])
    candidates.extend(analysis.get("key_facts") or [])
    description = re.sub(
        r"<[^>]+>", " ", html.unescape(str(article.get("original_description") or ""))
    )
    candidates.extend(re.split(r"(?<=[.!?])\s+|\s*[;•]\s*", description))
    result = []
    seen = set()
    for value in candidates:
        cleaned = " ".join(str(value or "").split()).strip(" •-")[:240]
        key = cleaned.casefold()
        if len(cleaned) < 12 or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) == 5:
            break
    source_name = " ".join(str(article.get("source_name") or "Nguồn bài gốc").split())
    if len(result) < 3:
        result.append(f"Thông tin và bối cảnh đầy đủ được dẫn từ {source_name}.")
    if len(result) < 3:
        result.append("Độc giả có thể kiểm tra thêm dữ kiện tại liên kết bài gốc.")
    return result[:5]


def _fallback_write_post(article: dict, analysis: dict, circle: dict) -> dict | None:
    """Tạo bản nháp bảo thủ từ dữ kiện đã có, không thêm thông tin ngoài nguồn."""
    clean = lambda value: " ".join(re.sub(r"<[^>]+>", " ", html.unescape(str(value or ""))).split())
    original_title = clean(article.get("original_title"))
    source_name = clean(article.get("source_name")) or "Nguồn bài gốc"
    source_url = str(article.get("source_url") or "").strip()
    if not original_title or not source_url:
        return None

    raw_facts = analysis.get("key_facts") if isinstance(analysis.get("key_facts"), list) else []
    facts = []
    for fact in raw_facts:
        cleaned = clean(fact)[:240]
        if cleaned and cleaned.casefold() not in {item.casefold() for item in facts}:
            facts.append(cleaned)
        if len(facts) >= 3:
            break
    if not facts:
        description = clean(article.get("original_description"))
        if description:
            facts.append(description[:240])
    if not facts:
        return None
    while len(facts) < 3:
        additions = [
            f"Thông tin và bối cảnh đầy đủ được dẫn từ {source_name}",
            "Độc giả có thể kiểm tra thêm dữ kiện tại liên kết bài gốc",
        ]
        facts.append(additions[min(len(facts) - 1, len(additions) - 1)])

    summary_parts = [f"Theo {source_name}, {facts[0].rstrip('.')}."]
    summary_parts.extend(f"{fact.rstrip('.')}." for fact in facts[1:3])
    summary_parts.append(f"Nội dung được biên tập từ thông tin do {source_name} công bố và giữ nguyên các dữ kiện có trong nguồn.")
    summary = " ".join(summary_parts)
    hashtags = _normalize_hashtags([], article, analysis, circle)
    result = {
        "ai_title": original_title[:100],
        "ai_summary": summary,
        "bullet_points": _normalize_bullet_points(facts, article, analysis),
        "hashtags": hashtags,
        "generation_mode": "local_fallback",
    }
    result["full_content"] = _build_full_content(result, source_url, source_name)
    return result


def write_post(article: dict, analysis: dict, circle_id: str = "circle_tintuc24h",
               max_retries: int = 3) -> dict | None:
    """
    Dùng Gemini để viết bài tóm tắt mới.
    
    Returns:
        Dict bài đã viết hoặc None nếu thất bại
    """
    title = article.get("original_title", "")[:60]
    logger.info(f"✍️ Writing: {title}...")

    circle = _get_circle_config(circle_id)
    prompt = _build_prompt(article, analysis, circle)

    config_path = Path(__file__).parent.parent / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        ai_config = json.load(f).get("ai", {})

    for attempt in range(1, max_retries + 1):
        try:
            result = generate_json(
                prompt,
                temperature=0.5,
                purpose="biên tập bài",
                preferred_model=ai_config.get("model_text"),
            )

            required = ["ai_title", "ai_summary", "bullet_points", "hashtags"]
            if not all(k in result for k in required):
                logger.warning(f"⚠️ Writer JSON thiếu trường (attempt {attempt})")
                continue

            result["bullet_points"] = _normalize_bullet_points(
                result.get("bullet_points"), article, analysis
            )
            result["hashtags"] = _normalize_hashtags(
                result.get("hashtags"), article, analysis, circle
            )
            # Luôn dựng full_content ở code để không thiếu mục hoặc sai số hashtag.
            result["full_content"] = _build_full_content(
                result,
                article.get("source_url", ""),
                article.get("source_name", "")
            )

            logger.info(f"  ✅ Written: '{result.get('ai_title', '')[:50]}'")
            time.sleep(1)
            return result

        except Exception as e:
            logger.error(f"❌ Writer lỗi (attempt {attempt}): {e}")
            error_text = str(e).casefold()
            if any(marker in error_text for marker in ("429", "resource_exhausted", "503", "unavailable")):
                break
            if attempt < max_retries:
                time.sleep(5 * attempt)

    fallback = _fallback_write_post(article, analysis, circle)
    if fallback:
        logger.warning(f"Gemini chưa sẵn sàng; đã tạo bản nháp dự phòng cho '{title}'.")
        return fallback
    logger.error(f"❌ Không thể tạo bản nháp dự phòng: {title}")
    return None
