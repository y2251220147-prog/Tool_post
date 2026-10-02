"""
Quản lý ảnh cho bài đăng.

Ưu tiên ảnh đại diện của chính bài báo để ảnh luôn đúng chủ đề. Nếu không lấy được
ảnh nguồn, hệ thống mới tạo một ảnh AI theo nội dung bài và cuối cùng lưu bản ghi
PENDING thay vì gắn nhầm một ảnh không liên quan.
"""

import io
import hashlib
import json
import re
import urllib.parse
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .gemini_client import generate_json, generate_image
from .image_context import image_context, image_context_hash
from .logger import get_logger
from .source_resolver import resolve_source_url
from .storage import save_media_asset, get_latest_image_asset

logger = get_logger("image_generator")

ROOT_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = ROOT_DIR / "prompts" / "image_prompt.md"
CONFIG_PATH = ROOT_DIR / "config" / "automation.json"
IMAGES_DIR = ROOT_DIR / "data" / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoUpFeed/2.0",
    "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/*,*/*;q=0.8",
}


class _ArticleImageParser(HTMLParser):
    """Tìm ảnh đại diện theo Open Graph, Twitter Card hoặc image_src."""

    def __init__(self):
        super().__init__()
        self.candidates: list[tuple[int, str]] = []
        self.embedded_pages: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = {str(key).lower(): value for key, value in attrs if key and value}
        if tag.lower() == "meta":
            marker = str(values.get("property") or values.get("name") or "").lower()
            content = str(values.get("content") or "").strip()
            priorities = {"og:image:secure_url": 0, "og:image": 1, "twitter:image": 2, "twitter:image:src": 3}
            if content and marker in priorities:
                self.candidates.append((priorities[marker], content))
        elif tag.lower() == "link":
            if str(values.get("rel", "")).lower() == "image_src" and values.get("href"):
                self.candidates.append((4, str(values["href"]).strip()))
        elif tag.lower() == "video" and values.get("poster"):
            self.candidates.append((0, str(values["poster"]).strip()))
        elif tag.lower() == "iframe" and values.get("src"):
            self.embedded_pages.append(str(values["src"]).strip())
        elif tag.lower() == "img":
            # Nhiều báo tải ảnh chính theo kiểu lazy-load và để URL trong data-src.
            marker = " ".join(str(values.get(key) or "") for key in ("class", "id", "alt", "title")).casefold()
            if any(word in marker for word in ("logo", "avatar", "icon", "sprite", "qr-code", "qrcode", "advert")):
                return
            candidates = []
            for key in ("data-original", "data-src", "data-lazy-src", "data-image", "src"):
                value = str(values.get(key) or "").strip()
                if value and not value.startswith("data:"):
                    candidates.append(value)
            srcset = str(values.get("srcset") or "").strip()
            if srcset:
                largest = srcset.split(",")[-1].strip().split()
                if largest:
                    candidates.insert(0, largest[0])
            try:
                width = int(str(values.get("width") or "0").replace("px", ""))
                height = int(str(values.get("height") or "0").replace("px", ""))
            except ValueError:
                width = height = 0
            if width and height and (width < 320 or height < 180):
                return
            priority = 10
            if any(word in marker for word in ("article", "content", "hero", "cover", "main", "detail")):
                priority -= 3
            if width >= 640 and height >= 360:
                priority -= 2
            for candidate in candidates:
                self.candidates.append((priority, candidate))

    def best_url(self, page_url: str) -> str:
        for _, candidate in sorted(self.candidates, key=lambda item: item[0]):
            absolute = urljoin(page_url, candidate)
            lowered_path = urllib.parse.urlparse(absolute).path.casefold()
            is_site_decoration = any(
                marker in lowered_path
                for marker in ("/logo", "logo-", "logo_", "/icon", "favicon", "sprite", "avatar")
            )
            if absolute.startswith(("http://", "https://")) and not is_site_decoration:
                return absolute
        return ""

    def embed_urls(self, page_url: str) -> list[str]:
        result = []
        for candidate in self.embedded_pages:
            absolute = urljoin(page_url, candidate)
            if absolute.startswith(("http://", "https://")) and absolute not in result:
                result.append(absolute)
        return result[:3]


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_prompt_template() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _build_image_prompt_request(post_data: dict, analysis: dict, circle: dict | None = None,
                                retry_feedback: str = "", max_prompt_chars: int = 4000) -> str:
    return (
        _load_prompt_template()
        .replace("{{image_context}}", json.dumps(image_context(post_data, analysis, circle or {}), ensure_ascii=False))
        .replace("{{retry_feedback}}", json.dumps(retry_feedback, ensure_ascii=False))
        .replace("{{max_prompt_chars}}", str(max_prompt_chars))
    )


def extract_source_image_url(html: str, page_url: str) -> str:
    """Tách URL ảnh chính từ HTML bài báo."""
    parser = _ArticleImageParser()
    parser.feed(html or "")
    return parser.best_url(page_url)


def extract_embedded_page_urls(html: str, page_url: str) -> list[str]:
    """Lấy trang trình phát nhúng để tìm poster gốc của bài video."""
    parser = _ArticleImageParser()
    parser.feed(html or "")
    return parser.embed_urls(page_url)


def _discover_source_image_url(post_data: dict, timeout_seconds: float) -> str:
    # Video YouTube đã có thumbnail chính thức từ API, không cần mở lại trang.
    for candidate in (post_data.get("source_image_url"), post_data.get("thumbnail_url")):
        image_url = str(candidate or "").strip()
        if image_url.startswith(("http://", "https://")):
            return image_url

    source_url = str(post_data.get("source_url") or "").strip()
    if not source_url.startswith(("http://", "https://")):
        return ""

    source_url = resolve_source_url(source_url, timeout_seconds=timeout_seconds)

    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS) as client:
            response = client.get(source_url)
            response.raise_for_status()
            page_url = str(response.url)
            image_url = extract_source_image_url(response.text, page_url)
            if image_url:
                return image_url
            for embed_url in extract_embedded_page_urls(response.text, page_url):
                try:
                    embed_response = client.get(embed_url, headers={**REQUEST_HEADERS, "Referer": page_url})
                    embed_response.raise_for_status()
                    image_url = extract_source_image_url(embed_response.text, str(embed_response.url))
                    if image_url:
                        return image_url
                except Exception as embed_exc:
                    logger.warning(f"Không đọc được trang video nhúng: {str(embed_exc)[:140]}")
        return ""
    except Exception as exc:
        logger.warning(f"Không lấy được trang bài gốc để tìm ảnh: {str(exc)[:180]}")
        return ""


def _save_normalized_image(image_bytes: bytes, output_path: Path):
    """Kiểm tra file thật sự là ảnh rồi chuẩn hóa về JPEG 16:9."""
    if len(image_bytes) < 1024:
        raise ValueError("Dữ liệu ảnh quá nhỏ")
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.load()
            if image.width < 320 or image.height < 180:
                raise ValueError(f"Ảnh nguồn quá nhỏ: {image.width}x{image.height}")
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            normalized = ImageOps.fit(normalized, (1280, 720), method=Image.Resampling.LANCZOS)
            normalized.save(output_path, format="JPEG", quality=90, optimize=True)
    except UnidentifiedImageError as exc:
        raise ValueError("URL không trả về file ảnh hợp lệ") from exc


def _download_source_image(post_id: int, post_data: dict, timeout_seconds: float) -> dict | None:
    image_url = _discover_source_image_url(post_data, timeout_seconds)
    if not image_url:
        return None
    source_url = str(post_data.get("source_url") or "")
    headers = {**REQUEST_HEADERS, "Referer": source_url} if source_url else REQUEST_HEADERS
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            response = client.get(image_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError(f"Content-Type không phải ảnh: {content_type}")

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        file_name = f"post_{post_id}_{timestamp}_source.jpg"
        file_path = IMAGES_DIR / file_name
        _save_normalized_image(response.content, file_path)
        logger.info(f"Đã tải ảnh nguồn; đang chờ kiểm tra nội dung: {file_name}")
        return {
            "file_name": file_name,
            "file_path": str(file_path),
            "source_url": image_url,
            "source_name": post_data.get("source_name", ""),
            "is_ai_generated": 0,
            "image_prompt": "",
            "image_label": f"🖼 Ảnh từ bài gốc — Nguồn: {post_data.get('source_name', 'Báo chí')}",
        }
    except Exception as exc:
        logger.warning(f"Không tải được ảnh đại diện của bài gốc: {str(exc)[:200]}")
        return None


def download_source_image_for_post(post_id: int, post_data: dict, timeout_seconds: float = 15) -> dict | None:
    """API công khai dùng cho công cụ bổ sung lại ảnh của các bài cũ."""
    return _download_source_image(post_id, post_data, timeout_seconds)


def generate_image_prompt(post_data: dict, analysis: dict, circle: dict | None = None,
                          retry_feedback: str = "", max_prompt_chars: int = 4000) -> dict:
    """Gemini đọc đầy đủ bài để thiết kế cảnh; không dùng prompt chung khi API lỗi."""
    request = _build_image_prompt_request(post_data, analysis, circle, retry_feedback, max_prompt_chars)
    result = generate_json(request, temperature=0.2, purpose="thiết kế ảnh theo toàn bài", include_model=True)
    prompt = str(result.get("image_prompt") or "").strip()
    if not prompt or len(prompt) > max_prompt_chars:
        raise ValueError("Prompt ảnh thiếu hoặc vượt giới hạn; giữ bài Chờ ảnh để xử lý tiếp.")
    result["image_prompt"] = prompt
    result["prompt_request"] = request
    return result


def _generate_pollinations_image(post_id: int, prompt_meta: dict, timeout_seconds: float) -> dict | None:
    prompt = str(prompt_meta.get("image_prompt") or "").strip()
    if not prompt:
        return None
    encoded = urllib.parse.quote(prompt, safe="")
    seed_input = f"{post_id}:{datetime.now().isoformat()}:{prompt}"
    seed = int(hashlib.sha256(seed_input.encode("utf-8")).hexdigest()[:12], 16) % 1_000_000
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1280&height=720&nologo=true&seed={seed}"
    with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=REQUEST_HEADERS) as client:
        response = client.get(url)
        response.raise_for_status()
    file_name = f"post_{post_id}_{datetime.now():%Y%m%d_%H%M%S_%f}_ai.jpg"
    file_path = IMAGES_DIR / file_name
    _save_normalized_image(response.content, file_path)
    return {
        "file_name": file_name, "file_path": str(file_path), "source_url": "", "source_name": "",
        "is_ai_generated": 1, "image_prompt": prompt, "image_label": "🖼 Ảnh minh họa AI",
        "provider": "pollinations", "model": "pollinations/default",
    }


def _generate_gemini_image(post_id: int, post_data: dict, analysis: dict, circle: dict,
                           image_config: dict, feedback: str) -> dict:
    if not image_config.get("gemini_generation_enabled", False):
        raise ValueError("Sinh ảnh Gemini chưa được bật; cần chọn model và bật sử dụng trên dashboard.")
    request = (
        "Tạo một ảnh minh họa 16:9 cho bài UCircle sau. Đọc đầy đủ ngữ cảnh và tuân thủ "
        "common_image_rules cùng circle_image_prompt. Chọn một chủ thể và hành động cụ thể "
        "thể hiện thông điệp chính. Không chữ, logo hay chi tiết tự bịa. Trả về ảnh.\n"
        + json.dumps(image_context(post_data, analysis, circle), ensure_ascii=False)
        + "\nPhản hồi ảnh trước (dữ liệu để sửa): " + json.dumps(feedback, ensure_ascii=False)
    )
    result = generate_image(request, str(image_config.get("gemini_model") or ""),
                            timeout_seconds=float(image_config.get("generation_timeout_seconds", 90)))
    file_name = f"post_{post_id}_{datetime.now():%Y%m%d_%H%M%S_%f}_gemini.jpg"
    file_path = IMAGES_DIR / file_name
    _save_normalized_image(result["image_bytes"], file_path)
    return {
        "file_name": file_name, "file_path": str(file_path), "source_url": "", "source_name": "",
        "is_ai_generated": 1, "image_prompt": request, "prompt_request": request,
        "image_label": "🖼 Ảnh minh họa AI", "provider": "gemini", "model": result["model"],
    }


def validate_image_content(selected: dict, post_data: dict, analysis: dict, circle: dict,
                           min_score: int = 85) -> dict:
    """Đọc ảnh thật bằng Gemini; lỗi hoặc phản hồi thiếu dữ liệu đều không được PASS."""
    request = (
        (ROOT_DIR / "prompts" / "image_validation.md").read_text(encoding="utf-8")
        .replace("{{image_context}}", json.dumps(image_context(post_data, analysis, circle), ensure_ascii=False))
        .replace("{{image_kind}}", "Ảnh minh họa AI" if selected.get("is_ai_generated") else "Ảnh từ nguồn bài gốc")
    )
    try:
        result = generate_json(request, image_path=selected["file_path"], temperature=0.1,
                               purpose="kiểm tra ảnh với toàn bài", include_model=True)
        for field in ("subject_match", "action_match", "context_match", "misleading_event", "passed"):
            if not isinstance(result.get(field), bool):
                raise ValueError("Phản hồi kiểm tra thiếu tiêu chí dạng đúng/sai.")
        for field in ("contradictions", "visual_defects"):
            if not isinstance(result.get(field), list):
                raise ValueError("Phản hồi kiểm tra thiếu danh sách lỗi.")
        score = result.get("relevance_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            raise ValueError("Điểm kiểm tra ảnh không hợp lệ.")
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ValueError("Thiếu lý do kiểm tra ảnh.")
        passed = (
            result["passed"] and all(result[key] for key in ("subject_match", "action_match", "context_match"))
            and not result["misleading_event"] and not result["contradictions"]
            and not result["visual_defects"] and score >= min_score
        )
        reason = result["reason"].strip()
        if not passed:
            reason += f" Điểm phù hợp {score}/100; yêu cầu từ {min_score} và không có tiêu chí sai."
        return {"status": "PASS" if passed else "REJECT", "reason": reason,
                "score": int(score), "result": result}
    except Exception as exc:
        logger.warning(f"Chưa xác minh được ảnh: {type(exc).__name__}")
        return {"status": "PENDING", "score": 0, "result": {},
                "reason": "Chưa kiểm tra được ảnh bằng Gemini (dịch vụ, hạn mức hoặc phản hồi không hợp lệ); giữ bài Chờ ảnh."}


def replace_image_label(full_content: str, image_label: str) -> str:
    """Đồng bộ nhãn ảnh trong nội dung đăng với loại ảnh đã lưu."""
    content = full_content or ""
    if re.search(r"^🖼.*$", content, flags=re.MULTILINE):
        return re.sub(r"^🖼.*$", image_label, content, count=1, flags=re.MULTILINE)
    return f"{content.rstrip()}\n{image_label}".strip()


def _asset_result(asset: dict, cached: bool = False) -> dict:
    label = "🖼 Ảnh minh họa AI" if asset.get("is_ai_generated", 1) else f"🖼 Ảnh từ bài gốc — Nguồn: {asset.get('source_name', '')}"
    return {
        "media_id": asset.get("id"), "file_path": asset.get("file_path", ""),
        "file_name": asset.get("file_name", ""), "image_prompt": asset.get("image_prompt", ""),
        "image_label": label if asset.get("file_path") else "🖼 Chưa có ảnh minh họa",
        "is_ai_generated": bool(asset.get("is_ai_generated", 1)),
        "status": asset.get("validation_status", "PENDING"),
        "reason": asset.get("validation_reason", ""), "score": asset.get("relevance_score", 0),
        "provider": asset.get("provider", ""), "model": asset.get("model", ""),
        "context_hash": asset.get("context_hash", ""), "cached": cached,
        "total_images": 1 if asset.get("file_path") else 0,
        "all_files": [asset["file_name"]] if asset.get("file_name") else [],
    }


def generate_and_save_image(post_id: int, post_data: dict, analysis: dict, num_images: int | None = None,
                            *, circle: dict | None = None, force_regenerate: bool = False,
                            control=None, force_source: bool = False) -> dict:
    """Tiếp tục từ ảnh đã lưu; tạo tối đa hai ảnh AI, chỉ PASS sau khi đọc ảnh thật."""
    if circle is None:
        from .ucircle_account import get_circle_config
        from .storage import get_post_by_id
        stored_post = get_post_by_id(post_id) or {}
        circle = get_circle_config(post_data.get("circle_id") or stored_post.get("circle_id")) or {}
    image_config = _load_config().get("images", {})
    strategy = str(circle.get("image_strategy") or image_config.get("strategy", "source_first")).lower()
    image_config = {**image_config, "strategy": strategy}
    if force_source:
        image_config = {**image_config, "strategy": "source_only", "allow_ai_fallback": False}
        force_regenerate = False
    fingerprint = image_context_hash(post_data, analysis, circle)
    threshold = max(0, min(100, int(image_config.get("min_relevance_score", 85))))
    previous = get_latest_image_asset(post_id)
    previous_has_file = bool(previous and previous.get("file_path") and Path(previous["file_path"]).is_file())
    if (not force_regenerate and not force_source and previous_has_file and previous.get("context_hash") == fingerprint
            and previous.get("validation_status") == "PASS" and previous.get("relevance_score", 0) >= threshold):
        return _asset_result(previous, cached=True)

    def persist(selected: dict | None, check: dict, attempt: int = 0, request: str = "") -> dict:
        asset = {
            "post_id": post_id, "type": "image", "file_path": "", "file_name": "",
            "image_prompt": "", "is_ai_generated": 1, "source_url": "", "source_name": "",
            "sensitivity_note": "", "provider": str(image_config.get("provider", "pollinations")),
            "model": str(image_config.get("gemini_model", "")) if image_config.get("provider") == "gemini" else "pollinations/default",
            **(selected or {}), "context_hash": fingerprint,
            "validation_status": check["status"], "validation_reason": check["reason"],
            "relevance_score": check.get("score", 0), "validation_result": check.get("result") or {},
            "generation_attempt": attempt, "prompt_request": request or (selected or {}).get("prompt_request", ""),
        }
        asset["id"] = save_media_asset(asset)
        return _asset_result(asset)

    def pending(reason: str, selected: dict | None = None) -> dict:
        if selected is None and previous and previous.get("context_hash") == fingerprint and previous.get("validation_status") == "PENDING":
            selected = dict(previous)
        return persist(selected, {"status": "PENDING", "reason": reason, "score": 0})

    def can_continue() -> bool:
        return control is None or bool(control())

    selected = None
    # Một ảnh đang chờ xác minh hoặc ảnh cũ sau khi sửa nội dung được đọc lại,
    # tránh tạo ảnh mới chỉ vì lần trước Gemini hết hạn mức.
    retry_source_before_ai = bool(
        strategy == "source_first" and previous_has_file
        and previous.get("is_ai_generated")
        and previous.get("validation_status") in {"PENDING", "REJECT"}
    )
    if not force_regenerate and not force_source and previous_has_file and not (
        strategy == "source_only" and previous.get("is_ai_generated")
    ) and not retry_source_before_ai and (
        previous.get("validation_status") != "REJECT" or previous.get("context_hash") != fingerprint
    ):
        selected = dict(previous)
    if not can_continue():
        return pending("Đã lưu bài và dừng trước bước ảnh; có thể tiếp tục xử lý ảnh.", selected)
    source_already_rejected = bool(
        strategy != "source_only" and not force_source and previous_has_file
        and not previous.get("is_ai_generated")
        and previous.get("context_hash") == fingerprint
        and previous.get("validation_status") == "REJECT"
    )
    source_rejected_this_run = False
    if selected is None and strategy in ("source_first", "source_only") and not force_regenerate and not source_already_rejected:
        selected = _download_source_image(post_id, post_data, float(image_config.get("download_timeout_seconds", 45)))
        if selected:
            selected.update({"provider": "source", "model": "ảnh bài gốc"})
    # Khi lần trước đã tạo ảnh AI nhưng việc xác minh bị gián đoạn, thử ảnh gốc
    # trước; nếu không tìm thấy thì tiếp tục xác minh ảnh AI đã lưu thay vì bỏ nó.
    if (selected is None and retry_source_before_ai and previous
            and previous.get("validation_status") == "PENDING"
            and previous.get("context_hash") == fingerprint):
        selected = dict(previous)

    feedback = ""
    latest_result = None
    if selected:
        if not can_continue():
            return pending("Ảnh đã lưu; chờ tiếp tục kiểm tra phù hợp nội dung.", selected)
        check = validate_image_content(selected, post_data, analysis, circle, threshold)
        latest_result = persist(selected, check, int(selected.get("generation_attempt", 0) or 0))
        if check["status"] in {"PASS", "PENDING"}:
            return latest_result
        source_rejected_this_run = not bool(selected.get("is_ai_generated", 1))
        feedback = check["reason"]
    elif previous and previous.get("context_hash") == fingerprint:
        feedback = str(previous.get("validation_reason") or "")

    def retry_original_source() -> dict | None:
        """Khi AI không thể tạo ảnh, thử lại một lần lấy ảnh gốc của đúng bài."""
        if strategy == "source_only" or force_source or source_already_rejected or source_rejected_this_run:
            return None
        if not can_continue():
            return pending("Đã dừng trước lần thử dự phòng lấy ảnh gốc.")
        source_image = _download_source_image(
            post_id, post_data, float(image_config.get("download_timeout_seconds", 45))
        )
        if not source_image:
            return None
        source_image.update({"provider": "source", "model": "ảnh bài gốc"})
        source_check = validate_image_content(source_image, post_data, analysis, circle, threshold)
        source_result = persist(source_image, source_check)
        if source_check["status"] == "PASS":
            logger.info(f"Đã dùng ảnh gốc làm phương án dự phòng cho bài #{post_id}.")
        elif source_check["status"] == "PENDING":
            logger.info(f"Đã lưu ảnh gốc dự phòng cho bài #{post_id}; chờ xác minh ảnh.")
        else:
            logger.info(f"Ảnh gốc dự phòng của bài #{post_id} không đạt kiểm tra; tiếp tục giữ bài Chờ ảnh.")
        return source_result

    if strategy == "source_only" or not image_config.get("allow_ai_fallback", True):
        if source_already_rejected and not force_regenerate:
            return _asset_result(previous)
        return latest_result or pending("Không có ảnh nguồn hợp lệ; bài được giữ ở Chờ ảnh.")
    provider = str(image_config.get("provider", "pollinations"))
    if provider not in {"pollinations", "gemini"}:
        source_result = retry_original_source()
        if source_result:
            return source_result
        return pending("Dịch vụ tạo ảnh chưa được cấu hình đúng.")
    attempts = max(1, min(2, int(image_config.get("max_generation_attempts", 2))))
    for attempt in range(1, attempts + 1):
        if not can_continue():
            return pending("Đã lưu tiến trình ảnh; chờ tiếp tục.", selected)
        prompt_meta = {}
        try:
            if provider == "gemini":
                selected = _generate_gemini_image(post_id, post_data, analysis, circle, image_config, feedback)
            else:
                reuse_prompt = bool(attempt == 1 and not force_regenerate and previous
                                    and not previous_has_file and previous.get("context_hash") == fingerprint
                                    and previous.get("validation_status") == "PENDING"
                                    and previous.get("provider") == provider and previous.get("image_prompt"))
                prompt_meta = ({"image_prompt": previous["image_prompt"],
                                "prompt_request": previous.get("prompt_request", "")}
                               if reuse_prompt else generate_image_prompt(post_data, analysis, circle, feedback,
                                                                          int(image_config.get("max_prompt_chars", 4000))))
                if not can_continue():
                    return pending("Đã thiết kế ảnh; chờ tiếp tục tạo ảnh.", {
                        "image_prompt": prompt_meta["image_prompt"], "prompt_request": prompt_meta.get("prompt_request", ""),
                    })
                selected = _generate_pollinations_image(
                    post_id, prompt_meta, float(image_config.get("generation_timeout_seconds", 90))
                )
            if not selected:
                raise ValueError("Dịch vụ không trả ảnh hợp lệ.")
            if prompt_meta:
                selected["prompt_request"] = prompt_meta.get("prompt_request", "")
                selected["sensitivity_note"] = prompt_meta.get("sensitivity_note", "")
            if not can_continue():
                return pending("Ảnh đã tạo và lưu; chờ tiếp tục kiểm tra.", selected)
            check = validate_image_content(selected, post_data, analysis, circle, threshold)
            latest_result = persist(selected, check, attempt)
            if check["status"] == "PASS":
                return latest_result
            if check["status"] == "PENDING":
                source_result = retry_original_source()
                return source_result or latest_result
            feedback = check["reason"]
        except Exception as exc:
            logger.warning(f"Bước ảnh #{post_id} chưa hoàn tất: {type(exc).__name__}")
            source_result = retry_original_source()
            if source_result:
                return source_result
            if selected is None and prompt_meta:
                selected = {"image_prompt": prompt_meta.get("image_prompt", ""),
                            "prompt_request": prompt_meta.get("prompt_request", "")}
            return pending(
                str(exc) if isinstance(exc, ValueError) else
                "Dịch vụ thiết kế/tạo ảnh chưa sẵn sàng; bài và ảnh đã có được giữ lại để tiếp tục.",
                selected,
            )
    source_result = retry_original_source()
    return source_result or latest_result or pending("Chưa có ảnh đạt; giữ bài Chờ ảnh để thử lại sau.")
