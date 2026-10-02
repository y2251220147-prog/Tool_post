"""Nạp Prompt chung và hồ sơ prompt Circle từ cấu hình ngoài mã nguồn."""

import json
import re
import unicodedata
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
PROFILES_FILE = ROOT_DIR / "config" / "prompt_profiles.json"


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _read_prompt_file(relative_path: str) -> str:
    path = (ROOT_DIR / str(relative_path or "")).resolve()
    prompt_root = (ROOT_DIR / "config" / "prompts").resolve()
    try:
        path.relative_to(prompt_root)
    except ValueError:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or "").replace("Đ", "D").replace("đ", "d"))
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def load_prompt_profiles() -> dict:
    """Đọc mới mỗi lần để thay file prompt có hiệu lực mà không sửa code."""
    config = _read_json(PROFILES_FILE, {"profiles": []})
    common_prompt = _read_prompt_file(config.get("common_prompt_file", ""))
    selection_prompt = _read_prompt_file(config.get("selection_prompt_file", ""))
    profiles = []
    for raw in config.get("profiles", []):
        if not isinstance(raw, dict) or not raw.get("profile_id"):
            continue
        profile = dict(raw)
        profile["content_prompt"] = _read_prompt_file(profile.get("prompt_file", ""))
        profile["image_prompt"] = _read_prompt_file(profile.get("image_prompt_file", "")) or str(profile.get("image_prompt", ""))
        profiles.append(profile)
    return {
        "version": config.get("version", 1),
        "common_prompt": common_prompt,
        "selection_prompt": selection_prompt,
        "default_profile_id": config.get("default_profile_id", "generic"),
        "profiles": profiles,
    }


def get_prompt_profile(profile_id: str) -> dict:
    data = load_prompt_profiles()
    for profile in data["profiles"]:
        if profile.get("profile_id") == profile_id:
            result = dict(profile)
            result["common_prompt"] = data["common_prompt"]
            result["selection_prompt"] = data["selection_prompt"]
            return result
    return {}


def match_prompt_profile(circle_name: str, category_name: str = "") -> dict:
    data = load_prompt_profiles()
    normalized_name = _normalize(circle_name)
    normalized_category = _normalize(category_name)
    best_profile = None
    best_score = -1
    for profile in data["profiles"]:
        if profile.get("profile_id") == data["default_profile_id"]:
            continue
        aliases = [_normalize(item) for item in profile.get("aliases", []) if str(item).strip()]
        category_keywords = [
            _normalize(item) for item in profile.get("category_keywords", []) if str(item).strip()
        ]
        score = 0
        if normalized_name in aliases:
            score = 100
        else:
            score += max(
                (55 + min(len(alias), 25) for alias in aliases if alias and alias in normalized_name),
                default=0,
            )
            score += sum(8 for keyword in category_keywords if keyword and keyword in normalized_category)
        if score > best_score:
            best_score = score
            best_profile = profile
    if best_profile is None or best_score <= 0:
        best_profile = next(
            (item for item in data["profiles"] if item.get("profile_id") == data["default_profile_id"]),
            {},
        )
    result = dict(best_profile)
    result["common_prompt"] = data["common_prompt"]
    result["selection_prompt"] = data["selection_prompt"]
    return result


def resolve_circle_prompt(circle: dict) -> dict:
    """Ghép cấu hình Circle với profile; các trường *_override luôn được ưu tiên."""
    resolved = dict(circle or {})
    circle_name = str(resolved.get("display_name") or resolved.get("circle_name") or "Circle mới")
    category_name = str(
        resolved.get("ucircle_category_name") or resolved.get("category_name") or ""
    )
    configured_profile_id = str(resolved.get("prompt_profile_id") or "").strip()
    profile = get_prompt_profile(configured_profile_id) if configured_profile_id else match_prompt_profile(circle_name, category_name)
    if not profile:
        profile = match_prompt_profile(circle_name, category_name)
    profile_id = str(profile.get("profile_id") or "generic")

    def render(value: str) -> str:
        return (
            str(value or "")
            .replace("{{circle_name}}", circle_name)
            .replace("{{category_name}}", category_name or "chưa phân loại")
        )

    prompt_override = str(resolved.get("content_prompt_override") or "").strip()
    # Tương thích cấu hình cũ chưa có profile; khi đã có profile thì prompt nằm ngoài.
    legacy_prompt = str(resolved.get("content_prompt") or "").strip() if not configured_profile_id else ""
    search_override = str(resolved.get("media_search_query_override") or "").strip()
    legacy_search = str(resolved.get("media_search_query") or "").strip() if not configured_profile_id else ""
    resolved.update({
        "prompt_profile_id": profile_id,
        "prompt_profile_name": profile.get("display_name") or profile_id,
        "common_prompt": render(profile.get("common_prompt", "")),
        "selection_prompt": render(profile.get("selection_prompt", "")),
        "content_prompt": render(prompt_override or legacy_prompt or profile.get("content_prompt", "")),
        "image_prompt": render(resolved.get("image_prompt_override") or profile.get("image_prompt", "")),
        "image_strategy": str(
            resolved.get("image_strategy_override") or profile.get("image_strategy") or "source_first"
        ).strip().lower(),
        "media_search_query": render(
            search_override or legacy_search or profile.get("media_search_query", "") or circle_name
        ),
        "media_search_variants": [
            render(item) for item in (
                resolved.get("media_search_variants_override")
                or profile.get("media_search_variants")
                or resolved.get("media_search_variants")
                or []
            ) if str(item).strip()
        ],
        "media_max_age_days": int(
            resolved.get("media_max_age_days_override")
            or profile.get("media_max_age_days")
            or resolved.get("media_max_age_days")
            or 0
        ),
        "analysis_publish_threshold": int(
            resolved.get("analysis_publish_threshold_override")
            or profile.get("analysis_publish_threshold")
            or resolved.get("analysis_publish_threshold")
            or 0
        ),
        "analysis_allow_threshold_override": bool(
            resolved.get("analysis_allow_threshold_override")
            if resolved.get("analysis_allow_threshold_override") is not None
            else profile.get("analysis_allow_threshold_override", False)
        ),
        "fallback_filter": resolved.get("fallback_filter_override") or profile.get("fallback_filter") or {},
        "prompt_source": "circle_override" if prompt_override else "external_profile",
    })
    return resolved
