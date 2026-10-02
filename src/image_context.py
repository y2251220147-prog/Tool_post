"""Ngữ cảnh ảnh đầy đủ và dấu vân tay của phiên bản bài viết."""

import hashlib
import json
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
IMAGE_RULES_PATH = ROOT_DIR / "config" / "prompts" / "image_common.md"


def image_analysis(post: dict) -> dict:
    value = post.get("image_analysis") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = {}
    return value if isinstance(value, dict) else {}


def image_context(post: dict, analysis: dict, circle: dict) -> dict:
    # Nhãn ảnh được cập nhật sau khi sinh ảnh nên không phải một thay đổi nội dung.
    full_content = re.sub(r"^🖼[^\n]*\n?", "", str(post.get("full_content") or ""), flags=re.MULTILINE)
    return {
        "version": 1,
        "circle_name": circle.get("display_name") or circle.get("circle_name") or post.get("circle_id", ""),
        "circle_content_prompt": circle.get("content_prompt", ""),
        "circle_image_prompt": circle.get("image_prompt", ""),
        "image_strategy": circle.get("image_strategy", "source_first"),
        "common_image_rules": IMAGE_RULES_PATH.read_text(encoding="utf-8"),
        "article": {
            "title": post.get("ai_title", ""),
            "summary": post.get("ai_summary", ""),
            "bullet_points": post.get("bullet_points") or [],
            "hashtags": post.get("hashtags") or [],
            "full_content": full_content.strip(),
            "source_name": post.get("source_name", ""),
            "source_url": post.get("source_url", ""),
        },
        "analysis": {
            "topic": analysis.get("topic", ""),
            "key_facts": analysis.get("key_facts") or [],
            "is_sensitive": bool(analysis.get("is_sensitive", False)),
            "sensitivity_level": analysis.get("sensitivity_level", "low"),
        },
    }


def image_context_hash(post: dict, analysis: dict, circle: dict) -> str:
    encoded = json.dumps(image_context(post, analysis, circle), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def current_image_is_approved(post: dict, circle: dict) -> bool:
    """Ảnh chỉ dùng để đăng khi đã đọc ảnh và khớp phiên bản nội dung hiện tại."""
    if circle.get("image_strategy") == "source_only" and post.get("is_ai_generated"):
        return False
    path = post.get("image_path")
    if not path or not Path(str(path)).is_file() or post.get("image_validation_status") != "PASS":
        return False
    try:
        config = json.loads((ROOT_DIR / "config" / "automation.json").read_text(encoding="utf-8"))
        threshold = max(0, min(100, int(config.get("images", {}).get("min_relevance_score", 85))))
        if int(post.get("image_relevance_score") or 0) < threshold:
            return False
        fingerprint = post.get("image_context_hash")
        return bool(fingerprint) and fingerprint == image_context_hash(post, image_analysis(post), circle)
    except (OSError, ValueError, TypeError, AttributeError):
        # Không xác định được cấu hình/ngữ cảnh thì không cho phép đăng.
        return False
