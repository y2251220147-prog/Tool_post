"""
Tiện ích gọi Gemini dùng chung cho Analyzer, Writer và Image Prompt.
Tự phát hiện model hỗ trợ generateContent để tránh phụ thuộc vào một tên model cố định.
"""

import json
import os
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from .logger import get_logger

load_dotenv()

logger = get_logger("gemini_client")
ROOT_DIR = Path(__file__).resolve().parent.parent
_GEMINI_GATE = threading.BoundedSemaphore(value=3)
_GEMINI_STATE_LOCK = threading.Lock()
_GEMINI_BLOCKED_UNTIL = 0.0
_GEMINI_BLOCK_REASON = ""


def _create_client(api_key: str, timeout_seconds: float = 20):
    """Tạo client có timeout ngắn và tắt retry ngầm của SDK."""
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def _open_circuit(seconds: int, reason: str) -> None:
    global _GEMINI_BLOCKED_UNTIL, _GEMINI_BLOCK_REASON
    with _GEMINI_STATE_LOCK:
        _GEMINI_BLOCKED_UNTIL = max(_GEMINI_BLOCKED_UNTIL, time.monotonic() + seconds)
        _GEMINI_BLOCK_REASON = reason


def _circuit_error() -> str:
    with _GEMINI_STATE_LOCK:
        remaining = int(max(0, _GEMINI_BLOCKED_UNTIL - time.monotonic()))
        if remaining <= 0:
            return ""
        return f"Gemini tạm nghỉ {remaining} giây do {_GEMINI_BLOCK_REASON}; dùng chế độ dự phòng."


def _load_ai_config() -> dict:
    with open(ROOT_DIR / "config" / "automation.json", encoding="utf-8") as f:
        return json.load(f).get("ai", {})


def parse_json_response(text: str) -> dict | None:
    """Tách một JSON object từ phản hồi model."""
    cleaned = re.sub(r"```json\s*", "", text or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None


@lru_cache(maxsize=2)
def _available_text_models(api_key: str) -> tuple[str, ...]:
    """Lấy model khả dụng từ chính tài khoản và lưu cache trong tiến trình."""
    client = _create_client(api_key)
    available = []
    try:
        for model in client.models.list(config={"page_size": 100}):
            actions = set(getattr(model, "supported_actions", None) or [])
            name = str(getattr(model, "name", "") or "")
            normalized = name.removeprefix("models/")
            if "generateContent" not in actions or not normalized:
                continue
            lowered = normalized.lower()
            if any(marker in lowered for marker in ("embedding", "image", "imagen", "live", "tts", "veo")):
                continue
            available.append(normalized)
    except Exception as exc:
        logger.warning(f"Không thể lấy danh sách model Gemini: {str(exc)[:180]}")
    return tuple(available)


def _model_candidates(api_key: str, preferred_model: str | None = None) -> list[str]:
    config = _load_ai_config()
    configured = preferred_model or config.get("model_text", "")
    fallbacks = config.get("fallback_models", [])
    if isinstance(fallbacks, str):
        fallbacks = [fallbacks]

    available = list(_available_text_models(api_key))
    desired = [configured, *fallbacks]
    candidates = []
    for model_name in desired:
        normalized = str(model_name or "").removeprefix("models/").strip()
        if normalized and normalized not in candidates and (not available or normalized in available):
            candidates.append(normalized)

    if not candidates and available:
        flash_models = [m for m in available if "flash" in m.lower() and "preview" not in m.lower()]
        candidates.extend((flash_models or available)[:3])
    return candidates[:4]


def generate_json(
    prompt: str,
    *,
    temperature: float = 0.3,
    purpose: str = "nội dung",
    preferred_model: str | None = None,
    media_url: str | None = None,
    image_path: str | None = None,
    include_model: bool = False,
) -> dict:
    """Gọi lần lượt các model khả dụng cho tới khi nhận được JSON hợp lệ."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY chưa được cấu hình trong .env")

    from google.genai import types

    blocked_error = _circuit_error()
    if blocked_error:
        raise RuntimeError(blocked_error)

    client = _create_client(api_key)
    candidates = _model_candidates(api_key, preferred_model=preferred_model)
    if not candidates:
        raise RuntimeError("Không tìm thấy model Gemini nào hỗ trợ generateContent")

    contents = prompt
    if image_path:
        image_file = Path(image_path)
        if not image_file.is_file() or image_file.stat().st_size > 20_000_000:
            raise ValueError("Ảnh cần kiểm tra không tồn tại hoặc vượt 20 MB.")
        contents = types.Content(parts=[
            types.Part.from_bytes(data=image_file.read_bytes(), mime_type="image/jpeg"),
            types.Part(text=prompt),
        ])
    if media_url and media_url.startswith(("https://www.youtube.com/", "https://youtu.be/")):
        contents = types.Content(parts=[
            types.Part(file_data=types.FileData(file_uri=media_url)),
            types.Part(text=prompt),
        ])

    errors = []
    had_unavailable = False
    # Giới hạn ba yêu cầu đồng thời. SDK không tự retry để tránh treo giao diện nhiều phút.
    with _GEMINI_GATE:
        for model_name in candidates:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        response_mime_type="application/json",
                    ),
                )
                parsed = parse_json_response(response.text or "")
                if parsed is not None:
                    if include_model:
                        parsed["_model"] = model_name
                    media_note = " + video YouTube" if media_url else ""
                    logger.info(f"Gemini {purpose}: dùng model {model_name}{media_note}")
                    return parsed
                errors.append(f"{model_name}: phản hồi không phải JSON")
            except Exception as exc:
                message = str(exc)
                lowered = message.casefold()
                errors.append(f"{model_name}: {message[:180]}")
                if "429" in lowered or "resource_exhausted" in lowered or "quota" in lowered:
                    _open_circuit(600, "hết hạn mức API")
                    raise RuntimeError("Gemini đã hết hạn mức API; chuyển sang chế độ dự phòng.") from exc
                if "503" in lowered or "unavailable" in lowered:
                    had_unavailable = True

    if had_unavailable:
        _open_circuit(60, "dịch vụ đang quá tải")
    raise RuntimeError("; ".join(errors[-3:]))


def generate_image(prompt: str, model: str, timeout_seconds: float = 90) -> dict:
    """Sinh ảnh Gemini trực tiếp khi người dùng đã bật model ảnh trong cấu hình."""
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY chưa được cấu hình.")
    if not model or "image" not in model.casefold():
        raise ValueError("Cần cấu hình model Gemini hỗ trợ xuất ảnh.")
    blocked_error = _circuit_error()
    if blocked_error:
        raise RuntimeError(blocked_error)
    client = _create_client(api_key, timeout_seconds=timeout_seconds)
    with _GEMINI_GATE:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="16:9"),
                ),
            )
        except Exception as exc:
            message = str(exc).casefold()
            if "429" in message or "resource_exhausted" in message:
                _open_circuit(600, "hết hạn mức API")
            elif "503" in message or "unavailable" in message:
                _open_circuit(60, "dịch vụ đang quá tải")
            raise RuntimeError("Gemini không sinh được ảnh; kiểm tra quyền model, hạn mức hoặc kết nối.") from exc
    for candidate in response.candidates or []:
        for part in getattr(candidate.content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            inline = getattr(part, "inline_data", None)
            if inline and str(inline.mime_type or "").startswith("image/") and inline.data:
                return {"image_bytes": inline.data, "model": model}
    raise RuntimeError("Gemini không trả về dữ liệu ảnh.")
