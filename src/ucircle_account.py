"""Quản lý phiên đăng nhập và quét Circle UCircle cho dashboard cục bộ."""

import asyncio
import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .logger import get_logger
from .prompt_profiles import get_prompt_profile, match_prompt_profile, resolve_circle_prompt


logger = get_logger("ucircle_account")

ROOT_DIR = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT_DIR / "session.json"
ALTERNATIVE_SESSION_FILE = Path(r"D:\Auto_tool\Fb-To-Ucricle\session.json")
CIRCLES_CONFIG_FILE = ROOT_DIR / "config" / "circles.json"
DISCOVERED_CIRCLES_FILE = ROOT_DIR / "data" / "discovered_circles.json"
UCIRCLE_APP_URL = "https://ucircle.net/app"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _usable_session_path() -> Path | None:
    for path in (SESSION_FILE, ALTERNATIVE_SESSION_FILE):
        try:
            if path.exists() and path.stat().st_size > 100:
                return path
        except OSError:
            continue
    return None


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def _configured_circle_map() -> dict[str, dict]:
    config = _read_json(CIRCLES_CONFIG_FILE, {"circles": []})
    result = {}
    for circle in config.get("circles", []):
        url = circle.get("ucircle_api_url") or circle.get("ucircle_circle_id") or ""
        remote_id = _remote_circle_id(url)
        if remote_id:
            result[remote_id] = resolve_circle_prompt(circle)
    return result


def get_circle_config(circle_id: str) -> dict:
    config = _read_json(CIRCLES_CONFIG_FILE, {"circles": []})
    for circle in config.get("circles", []):
        if circle.get("circle_id") == circle_id:
            return resolve_circle_prompt(circle)
    return {}


def _remote_circle_id(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host and host != "ucircle.net" and not host.endswith(".ucircle.net"):
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "app" and parts[1] == "c":
            return parts[2]
    except (TypeError, ValueError):
        pass
    return ""


def _safe_page_url(url: str) -> str:
    """Bỏ query/fragment để không đưa mã đăng nhập lên API trạng thái."""
    try:
        parsed = urlparse(url)
        return parsed._replace(query="", fragment="").geturl()
    except (TypeError, ValueError):
        return ""


def _clean_circle_name(raw_name: str, fallback: str) -> str:
    """Bỏ chữ avatar và thống kê thành viên/bài khỏi tên hiển thị."""
    name = " ".join((raw_name or "").split())
    name = re.sub(r"\s+\d[\d.,]*\s+thành viên(?:\s*·\s*\d[\d.,]*\s+bài)?\s*$", "", name, flags=re.IGNORECASE)
    parts = name.split()
    if len(parts) >= 2 and len(parts[0]) == 1:
        first_letter = parts[0].casefold()
        if parts[1][:1].casefold() == first_letter:
            name = " ".join(parts[1:])
    return (name or fallback)[:160]


def get_discovered_circles() -> list[dict]:
    payload = _read_json(DISCOVERED_CIRCLES_FILE, {"circles": []})
    circles = payload.get("circles", [])
    if not isinstance(circles, list):
        return []
    configured = _configured_circle_map()
    result = []
    for discovered in circles:
        item = dict(discovered)
        known = configured.get(item.get("remote_id", ""), {})
        if known:
            item.update({
                "configured": True,
                "local_circle_id": known.get("circle_id"),
                "enabled": bool(known.get("enabled", False)),
                "scan_enabled": bool(known.get("scan_enabled", False)),
                "publish_enabled": bool(known.get("publish_enabled", False)),
                "content_prompt": known.get("content_prompt", ""),
                "prompt_profile_id": known.get("prompt_profile_id", "generic"),
                "prompt_profile_name": known.get("prompt_profile_name", ""),
                "prompt_source": known.get("prompt_source", "external_profile"),
                "dashboard_visible": bool(known.get("dashboard_visible", True)),
                "display_name": known.get("display_name", ""),
                "media_platforms": known.get("media_platforms", ["news"]),
                "media_search_query": known.get("media_search_query", ""),
            })
        result.append(item)
    return result


def _circle_slug(name: str, remote_id: str) -> str:
    normalized = unicodedata.normalize("NFD", name.replace("Đ", "D").replace("đ", "d"))
    ascii_name = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
    return f"circle_{slug[:36]}_{remote_id[:8]}"


def _default_content_prompt(circle_name: str, category_name: str = "") -> str:
    category_text = f" thuộc chủ đề {category_name}" if category_name else ""
    return (
        f"Chỉ chọn và biên tập nội dung liên quan trực tiếp đến Circle "
        f"'{circle_name}'{category_text}. Không dùng tin lạc chủ đề, không đăng chéo "
        "nội dung của Circle khác. Ưu tiên thông tin mới, hữu ích, có nguồn rõ ràng."
    )


def _sync_owned_circle_configs(owned_circles: list[dict]) -> list[dict]:
    """Bổ sung Circle sở hữu vào config, mặc định tắt với Circle mới."""
    config = _read_json(CIRCLES_CONFIG_FILE, {"circles": []})
    configured_circles = config.get("circles", [])
    by_remote_id = {}
    for circle in configured_circles:
        remote_id = _remote_circle_id(
            circle.get("ucircle_api_url") or circle.get("ucircle_circle_id") or ""
        )
        if remote_id:
            by_remote_id[remote_id] = circle

    synced = []
    for discovered in owned_circles:
        remote_id = discovered["remote_id"]
        circle = by_remote_id.get(remote_id)
        if circle is None:
            profile = match_prompt_profile(
                discovered["circle_name"], discovered.get("category_name", "")
            )
            circle = {
                "circle_id": _circle_slug(discovered["circle_name"], remote_id),
                "circle_name": discovered["circle_name"],
                "description": "",
                "topics": [],
                "persona": {
                    "tone": "thân thiện, đúng chủ đề",
                    "style": "rõ ràng, hữu ích",
                    "emoji_level": "ít",
                    "forbidden": ["lạc chủ đề", "thông tin không có nguồn"],
                },
                "posts_per_day": 5,
                "auto_publish_score": 92,
                "review_score": 75,
                "reject_score": 74,
                "enabled": False,
                "scan_enabled": False,
                "publish_enabled": False,
                "dashboard_visible": True,
                "display_name": "",
                "media_platforms": ["news"],
                "prompt_profile_id": profile.get("profile_id", "generic"),
                "content_prompt_override": "",
                "media_search_query_override": "",
                "ucircle_circle_id": discovered["circle_url"],
                "mode": "manual_review",
            }
            configured_circles.append(circle)
            by_remote_id[remote_id] = circle
        else:
            circle.setdefault("scan_enabled", bool(circle.get("enabled", False)))
            circle.setdefault("publish_enabled", bool(circle.get("enabled", False)))
            circle.setdefault("dashboard_visible", True)
            circle.setdefault("display_name", "")
            circle.setdefault("media_platforms", ["news"])
            if not circle.get("prompt_profile_id"):
                circle.setdefault("content_prompt_override", circle.get("content_prompt", ""))
                circle.setdefault(
                    "media_search_query_override", circle.get("media_search_query", "")
                )
                profile = match_prompt_profile(
                    circle.get("display_name") or circle.get("circle_name") or discovered["circle_name"],
                    discovered.get("category_name", ""),
                )
                circle["prompt_profile_id"] = profile.get("profile_id", "generic")
            circle.setdefault("content_prompt_override", "")
            circle.setdefault("media_search_query_override", "")

        circle["circle_name"] = discovered["circle_name"]
        circle["ownership_verified"] = True
        circle["ucircle_role"] = "owner"
        circle["ucircle_format"] = discovered.get("format", "")
        circle["ucircle_category"] = discovered.get("category", "")
        circle["ucircle_category_name"] = discovered.get("category_name", "")
        circle["ucircle_member_count"] = discovered.get("member_count", 0)
        circle["ucircle_circle_id"] = discovered["circle_url"]
        circle["enabled"] = bool(circle.get("scan_enabled") or circle.get("publish_enabled"))

        effective_circle = resolve_circle_prompt(circle)
        enriched = dict(discovered)
        enriched.update({
            "configured": True,
            "local_circle_id": circle["circle_id"],
            "enabled": circle["enabled"],
            "scan_enabled": bool(circle.get("scan_enabled", False)),
            "publish_enabled": bool(circle.get("publish_enabled", False)),
            "content_prompt": effective_circle.get("content_prompt", ""),
            "prompt_profile_id": effective_circle.get("prompt_profile_id", "generic"),
            "prompt_profile_name": effective_circle.get("prompt_profile_name", ""),
            "prompt_source": effective_circle.get("prompt_source", "external_profile"),
            "dashboard_visible": bool(circle.get("dashboard_visible", True)),
            "display_name": circle.get("display_name", ""),
            "media_platforms": circle.get("media_platforms", ["news"]),
            "media_search_query": effective_circle.get("media_search_query", ""),
            "publish_access": "owner",
        })
        synced.append(enriched)

    config["circles"] = configured_circles
    _write_json_atomic(CIRCLES_CONFIG_FILE, config)
    return synced


def update_circle_settings(
    circle_id: str,
    *,
    scan_enabled: bool,
    publish_enabled: bool,
    content_prompt: str,
    display_name: str | None = None,
    media_platforms: list[str] | None = None,
    media_search_query: str | None = None,
    prompt_profile_id: str | None = None,
    image_prompt: str | None = None,
) -> dict:
    prompt = " ".join((content_prompt or "").split())
    if len(prompt) < 20:
        raise ValueError("Prompt chủ đề cần ít nhất 20 ký tự.")
    if len(prompt) > 4000:
        raise ValueError("Prompt chủ đề không được vượt quá 4.000 ký tự.")
    if image_prompt is not None and (not isinstance(image_prompt, str) or len(image_prompt) > 6000):
        raise ValueError("Hướng dẫn ảnh phải là văn bản, không vượt quá 6.000 ký tự.")

    config = _read_json(CIRCLES_CONFIG_FILE, {"circles": []})
    for circle in config.get("circles", []):
        if circle.get("circle_id") != circle_id:
            continue
        if not circle.get("ownership_verified"):
            raise PermissionError("Circle này chưa được xác minh quyền sở hữu.")
        circle["scan_enabled"] = bool(scan_enabled)
        circle["publish_enabled"] = bool(publish_enabled)
        if display_name is not None:
            cleaned_name = " ".join(display_name.split())
            if not cleaned_name:
                raise ValueError("Tên hiển thị Circle không được để trống.")
            if len(cleaned_name) > 160:
                raise ValueError("Tên hiển thị Circle không được vượt quá 160 ký tự.")
            circle["display_name"] = cleaned_name
        if prompt_profile_id is not None:
            selected_profile_id = str(prompt_profile_id).strip()
            if not get_prompt_profile(selected_profile_id):
                raise ValueError("Hồ sơ prompt đã chọn không tồn tại trong cấu hình ngoài.")
            circle["prompt_profile_id"] = selected_profile_id
        # So sánh với profile ngoài sau khi đổi tên/profile. Nếu giống nhau thì
        # xóa bản ghi đè để các lần sửa file Markdown sau tự có hiệu lực.
        circle_without_override = dict(circle)
        circle_without_override["content_prompt_override"] = ""
        profile_prompt = " ".join(
            str(resolve_circle_prompt(circle_without_override).get("content_prompt") or "").split()
        )
        circle["content_prompt_override"] = "" if prompt == profile_prompt else prompt
        if image_prompt is not None:
            base_circle = {**circle, "image_prompt_override": ""}
            profile_image_prompt = resolve_circle_prompt(base_circle).get("image_prompt", "").strip()
            circle["image_prompt_override"] = "" if image_prompt.strip() == profile_image_prompt else image_prompt.strip()
        if media_platforms is not None:
            allowed = {"news", "youtube", "facebook", "facebook_reel"}
            selected = [str(item) for item in media_platforms if str(item) in allowed]
            if not selected:
                raise ValueError("Cần chọn ít nhất một loại nguồn nội dung.")
            circle["media_platforms"] = list(dict.fromkeys(selected))
        if media_search_query is not None:
            cleaned_query = " ".join(str(media_search_query).split())
            if len(cleaned_query) > 180:
                raise ValueError("Từ khóa tìm media không được vượt quá 180 ký tự.")
            circle_without_query_override = dict(circle)
            circle_without_query_override["media_search_query_override"] = ""
            profile_query = " ".join(
                str(resolve_circle_prompt(circle_without_query_override).get("media_search_query") or "").split()
            )
            circle["media_search_query_override"] = (
                "" if cleaned_query == profile_query else cleaned_query
            )
        circle["enabled"] = bool(scan_enabled or publish_enabled)
        _write_json_atomic(CIRCLES_CONFIG_FILE, config)
        return resolve_circle_prompt(circle)
    raise KeyError(f"Không tìm thấy Circle '{circle_id}'.")


def set_circle_dashboard_visibility(circle_id: str, visible: bool) -> dict:
    """Ẩn hoặc hiện Circle trên Dashboard mà không xóa cấu hình và lịch sử."""
    config = _read_json(CIRCLES_CONFIG_FILE, {"circles": []})
    for circle in config.get("circles", []):
        if circle.get("circle_id") != circle_id:
            continue
        if not circle.get("ownership_verified"):
            raise PermissionError("Circle này chưa được xác minh quyền sở hữu.")
        circle["dashboard_visible"] = bool(visible)
        if not visible:
            # Circle bị ẩn không được tiếp tục quét hoặc đăng ngầm ngoài Dashboard.
            circle["scan_enabled"] = False
            circle["publish_enabled"] = False
            circle["enabled"] = False
        _write_json_atomic(CIRCLES_CONFIG_FILE, config)
        return resolve_circle_prompt(circle)
    raise KeyError(f"Không tìm thấy Circle '{circle_id}'.")


class UCircleAccountManager:
    """Giữ trình duyệt đăng nhập mở và nhận lệnh lưu từ dashboard."""

    def __init__(self):
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._save_event = threading.Event()
        self._cancel_event = threading.Event()
        self._state = "idle"
        self._message = "Sẵn sàng."
        self._page_url = ""
        self._updated_at = _now_text()

    def _set_state(self, state: str, message: str, page_url: str = "") -> None:
        with self._lock:
            self._state = state
            self._message = message
            if page_url:
                self._page_url = _safe_page_url(page_url)
            self._updated_at = _now_text()
        logger.info(f"UCircle login state={state}: {message}")

    def status(self) -> dict:
        session_path = _usable_session_path()
        with self._lock:
            thread_running = bool(self._thread and self._thread.is_alive())
            result = {
                "state": self._state,
                "message": self._message,
                "page_url": self._page_url,
                "updated_at": self._updated_at,
                "browser_open": thread_running,
                "session_exists": bool(session_path),
                "session_path": session_path.name if session_path else None,
                "session_updated_at": None,
                "discovered_count": len(get_discovered_circles()),
            }
        if session_path:
            try:
                result["session_updated_at"] = datetime.fromtimestamp(
                    session_path.stat().st_mtime
                ).strftime("%Y-%m-%d %H:%M:%S")
            except OSError:
                pass
        return result

    def start_login(self) -> tuple[bool, str]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False, "Cửa sổ đăng nhập UCircle đang mở."
            self._save_event.clear()
            self._cancel_event.clear()
            self._state = "starting"
            self._message = "Đang mở trình duyệt UCircle..."
            self._page_url = ""
            self._updated_at = _now_text()
            self._thread = threading.Thread(target=self._login_thread, daemon=True)
            self._thread.start()
        return True, "Đã yêu cầu mở trình duyệt đăng nhập UCircle."

    def request_save(self) -> tuple[bool, str]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False, "Chưa có cửa sổ đăng nhập nào đang mở."
            self._save_event.set()
        return True, "Đang kiểm tra đăng nhập và lưu session.json..."

    def cancel_login(self) -> tuple[bool, str]:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False, "Không có tiến trình đăng nhập đang chạy."
            self._cancel_event.set()
        return True, "Đã gửi yêu cầu hủy đăng nhập."

    def _login_thread(self) -> None:
        try:
            asyncio.run(self._login_flow())
        except Exception as exc:
            logger.error(f"Lỗi luồng đăng nhập UCircle: {exc}", exc_info=True)
            self._set_state("error", f"Không thể mở đăng nhập UCircle: {exc}")

    async def _login_flow(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self._set_state("error", "Chưa cài Playwright cho Python đang chạy dashboard.")
            return

        browser = None
        async with async_playwright() as playwright:
            try:
                try:
                    browser = await playwright.chromium.launch(
                        headless=False, args=["--start-maximized"]
                    )
                except Exception:
                    browser = await playwright.chromium.launch(
                        channel="msedge", headless=False, args=["--start-maximized"]
                    )

                context_args = {"no_viewport": True}
                session_path = _usable_session_path()
                if session_path:
                    context_args["storage_state"] = str(session_path)
                try:
                    context = await browser.new_context(**context_args)
                except Exception:
                    context = await browser.new_context(no_viewport=True)

                page = await context.new_page()
                await page.goto(UCIRCLE_APP_URL, timeout=60000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                self._set_state(
                    "waiting",
                    "Trình duyệt đã mở. Hãy đăng nhập, sau đó bấm 'Lưu phiên' trên dashboard.",
                    page.url,
                )

                while not self._cancel_event.is_set():
                    if page.is_closed():
                        self._set_state("error", "Cửa sổ UCircle đã bị đóng trước khi lưu phiên.")
                        return

                    if self._save_event.is_set():
                        self._save_event.clear()
                        current_url = page.url
                        is_app = "/app" in current_url
                        is_login = "login" in current_url.lower() or "sso" in current_url.lower()
                        if not is_app or is_login:
                            self._set_state(
                                "waiting",
                                "Bạn chưa đăng nhập xong. Hãy vào trang UCircle /app rồi bấm Lưu phiên lần nữa.",
                                current_url,
                            )
                            await asyncio.sleep(0.5)
                            continue

                        self._set_state("saving", "Đang lưu phiên đăng nhập...", current_url)
                        await context.storage_state(path=str(SESSION_FILE))
                        self._set_state(
                            "success",
                            "Đã lưu session.json. Bạn có thể quét danh sách Circle.",
                            current_url,
                        )
                        return

                    await asyncio.sleep(0.5)

                self._set_state("cancelled", "Đã hủy thao tác đăng nhập UCircle.")
            finally:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass


_scan_lock = threading.Lock()
_scan_status = {
    "running": False,
    "message": "Chưa quét Circle trong phiên này.",
    "updated_at": None,
}


def get_scan_status() -> dict:
    with _scan_lock:
        return dict(_scan_status)


def _set_scan_status(running: bool, message: str) -> None:
    with _scan_lock:
        _scan_status.update({
            "running": running,
            "message": message,
            "updated_at": _now_text(),
        })


async def _scan_circles_async() -> list[dict]:
    from playwright.async_api import async_playwright

    session_path = _usable_session_path()
    if not session_path:
        raise RuntimeError("Chưa có session.json. Hãy đăng nhập UCircle trên dashboard trước.")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(storage_state=str(session_path))
            page = await context.new_page()
            await page.goto(UCIRCLE_APP_URL, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(4000)
            if "login" in page.url.lower() or "sso" in page.url.lower():
                raise RuntimeError("Phiên UCircle đã hết hạn. Hãy đăng nhập lại trên dashboard.")

            async def extract_owned_cards() -> list[dict]:
                return await page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll(
                      '[data-circle-card="true"][data-role="owner"][data-relationship="owned"]'
                    )).map((card) => {
                      const nameNode = card.querySelector('h3');
                      const categoryNode = card.querySelector('[data-circle-category]');
                      const memberNode = card.querySelector('[data-member-count]');
                      return {
                        remote_id: card.getAttribute('data-circle-id') || '',
                        name: (nameNode?.innerText || card.getAttribute('aria-label') || '').trim(),
                        role: card.getAttribute('data-role') || '',
                        relationship: card.getAttribute('data-relationship') || '',
                        format: card.getAttribute('data-format') || '',
                        category: categoryNode?.getAttribute('data-circle-category') || '',
                        category_name: (categoryNode?.innerText || '').trim(),
                        member_count: Number(memberNode?.getAttribute('data-member-count') || 0)
                      };
                    })
                    """
                )

            owned_cards = await extract_owned_cards()

            # Nếu danh sách chưa hiển thị, thử mở mục quản lý Circle.
            if not owned_cards:
                navigation_labels = [
                    "Circle của tôi",
                    "Circle của bạn",
                    "Quản lý Circle",
                    "Circle",
                ]
                for label in navigation_labels:
                    controls = page.get_by_text(label, exact=True)
                    count = min(await controls.count(), 3)
                    for index in range(count):
                        try:
                            await controls.nth(index).click(timeout=3000)
                            await page.wait_for_timeout(2500)
                            owned_cards = await extract_owned_cards()
                            if not owned_cards:
                                owned_tabs = page.get_by_text(re.compile(r"^Sở hữu(?:\s|\d|$)"))
                                owned_tab_count = min(await owned_tabs.count(), 3)
                                for tab_index in range(owned_tab_count):
                                    try:
                                        await owned_tabs.nth(tab_index).click(timeout=3000)
                                        await page.wait_for_timeout(2500)
                                        owned_cards = await extract_owned_cards()
                                        if owned_cards:
                                            break
                                    except Exception:
                                        continue
                            if owned_cards:
                                break
                        except Exception:
                            continue
                    if owned_cards:
                        break

            if not owned_cards:
                raise RuntimeError(
                    "Không tìm thấy bảng Circle sở hữu trên UCircle. "
                    "Hãy mở mục quản lý Circle trong lần đăng nhập rồi quét lại."
                )
        finally:
            await browser.close()

    owned: dict[str, dict] = {}
    for item in owned_cards:
        remote_id = (item.get("remote_id") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{20,64}", remote_id):
            continue
        name = _clean_circle_name(item.get("name") or "", f"Circle {remote_id[:8]}")
        owned[remote_id] = {
            "remote_id": remote_id,
            "circle_name": name[:160],
            "circle_url": f"https://ucircle.net/app/c/{remote_id}",
            "role": "owner",
            "relationship": "owned",
            "format": item.get("format", ""),
            "category": item.get("category", ""),
            "category_name": item.get("category_name", ""),
            "member_count": int(item.get("member_count") or 0),
        }

    circles = _sync_owned_circle_configs(
        sorted(owned.values(), key=lambda item: item["circle_name"].lower())
    )
    _write_json_atomic(DISCOVERED_CIRCLES_FILE, {
        "scanned_at": _now_text(),
        "source": "ucircle_owned_cards_read_only",
        "owner_only": True,
        "circles": circles,
    })
    return circles


def scan_circles() -> list[dict]:
    with _scan_lock:
        if _scan_status["running"]:
            raise RuntimeError("Tiến trình quét Circle đang chạy.")
        _scan_status["running"] = True
        _scan_status["message"] = "Đang quét danh sách Circle từ tài khoản UCircle..."
        _scan_status["updated_at"] = _now_text()
    try:
        circles = asyncio.run(_scan_circles_async())
        _set_scan_status(False, f"Đã quét thấy {len(circles)} Circle bạn sở hữu.")
        return circles
    except Exception as exc:
        _set_scan_status(False, str(exc))
        raise


account_manager = UCircleAccountManager()
