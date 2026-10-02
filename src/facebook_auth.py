"""OAuth Facebook cho các Page do người dùng quản lý; không ghi token ra log/API."""

import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
AUTH_FILE = ROOT_DIR / "data" / "facebook_auth.json"
MEDIA_CONFIG_FILE = ROOT_DIR / "config" / "media_sources.json"
load_dotenv(ROOT_DIR / ".env", override=True)

_state_lock = threading.RLock()
_pending_states: dict[str, float] = {}


def _media_config() -> dict:
    try:
        with open(MEDIA_CONFIG_FILE, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _graph_version() -> str:
    return str(_media_config().get("facebook", {}).get("graph_api_version", "v23.0"))


def _redirect_uri() -> str:
    return os.getenv(
        "FACEBOOK_REDIRECT_URI",
        "http://localhost:8000/api/facebook/oauth/callback",
    ).strip()


def credentials_ready() -> bool:
    return bool(
        os.getenv("FACEBOOK_APP_ID", "").strip()
        and os.getenv("FACEBOOK_APP_SECRET", "").strip()
    )


def create_login_url() -> str:
    app_id = os.getenv("FACEBOOK_APP_ID", "").strip()
    if not credentials_ready():
        raise ValueError("Thiếu FACEBOOK_APP_ID hoặc FACEBOOK_APP_SECRET trong .env")
    state = secrets.token_urlsafe(32)
    now = time.time()
    with _state_lock:
        _pending_states.clear()
        _pending_states[state] = now + 600
    params = {
        "client_id": app_id,
        "redirect_uri": _redirect_uri(),
        "state": state,
        "response_type": "code",
        "scope": "pages_show_list,pages_read_engagement,pages_read_user_content",
    }
    return f"https://www.facebook.com/{_graph_version()}/dialog/oauth?{urlencode(params)}"


def _validate_state(state: str) -> None:
    with _state_lock:
        expires_at = _pending_states.pop(state, 0)
    if not state or expires_at < time.time():
        raise ValueError("Phiên đăng nhập Facebook không hợp lệ hoặc đã hết hạn.")


def _request_json(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=30)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.ok or payload.get("error"):
        message = payload.get("error", {}).get("message") or f"HTTP {response.status_code}"
        raise RuntimeError(f"Facebook API: {message}")
    return payload


def complete_login(code: str, state: str) -> dict:
    """Đổi OAuth code lấy token dài hạn và danh sách Page; chỉ trả metadata an toàn."""
    _validate_state(state)
    app_id = os.getenv("FACEBOOK_APP_ID", "").strip()
    app_secret = os.getenv("FACEBOOK_APP_SECRET", "").strip()
    version = _graph_version()
    token_payload = _request_json(
        f"https://graph.facebook.com/{version}/oauth/access_token",
        {
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": _redirect_uri(),
            "code": code,
        },
    )
    user_token = str(token_payload.get("access_token") or "")
    if not user_token:
        raise RuntimeError("Facebook không trả về access token.")

    try:
        long_payload = _request_json(
            f"https://graph.facebook.com/{version}/oauth/access_token",
            {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": user_token,
            },
        )
        user_token = str(long_payload.get("access_token") or user_token)
        expires_in = int(long_payload.get("expires_in") or token_payload.get("expires_in") or 0)
    except Exception:
        expires_in = int(token_payload.get("expires_in") or 0)

    accounts = _request_json(
        f"https://graph.facebook.com/{version}/me/accounts",
        {
            "access_token": user_token,
            "fields": "id,name,access_token,tasks",
            "limit": 100,
        },
    )
    pages = []
    for page in accounts.get("data", []):
        page_id = str(page.get("id") or "").strip()
        page_token = str(page.get("access_token") or "").strip()
        if page_id and page_token:
            pages.append({
                "page_id": page_id,
                "name": str(page.get("name") or page_id),
                "access_token": page_token,
                "tasks": page.get("tasks") or [],
            })

    payload = {
        "user_access_token": user_token,
        "expires_at": int(time.time()) + expires_in if expires_in else 0,
        "pages": pages,
        "updated_at": int(time.time()),
    }
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = AUTH_FILE.with_suffix(".json.tmp")
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temp_file.replace(AUTH_FILE)
    return {"connected": True, "page_count": len(pages), "pages": [p["name"] for p in pages]}


def get_authorized_pages() -> list[dict]:
    try:
        with open(AUTH_FILE, encoding="utf-8") as file:
            payload = json.load(file)
        pages = payload.get("pages", [])
        return pages if isinstance(pages, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def status() -> dict:
    pages = get_authorized_pages()
    return {
        "credentials_ready": credentials_ready(),
        "connected": bool(pages),
        "page_count": len(pages),
        "pages": [{"page_id": p.get("page_id"), "name": p.get("name")} for p in pages],
        "redirect_uri": _redirect_uri(),
    }


def disconnect() -> None:
    if AUTH_FILE.exists():
        AUTH_FILE.unlink()
