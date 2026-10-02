"""
src/publisher.py
Module 10 — UCircle Publisher
Xuất bản bài viết lên UCircle:
1. Hỗ trợ qua API chính thức nếu có cấu hình token
2. Adapter Playwright tự động hoá trình duyệt với phiên đăng nhập session.json
3. Cơ chế thử lại (Retry Strategy: 1m, 5m, 15m)
4. Ghi nhận Publish Log và cập nhật trạng thái bài viết
"""

import os
import time
import json
import asyncio
import threading
import re
import unicodedata
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from .storage import (
    save_publish_log,
    update_post_status,
    get_post_by_id,
    get_all_media_assets_by_post,
    claim_post_for_publish,
)
from .queue_manager import can_publish_now
from .logger import get_logger
from .ucircle_account import get_circle_config
from .image_context import current_image_is_approved

load_dotenv()
logger = get_logger("publisher")

_publish_error_lock = threading.Lock()
_session_state_lock = threading.Lock()
_publish_errors: dict[int, str] = {}


def _set_publish_error(post_id: int, message: str | None) -> None:
    """Lưu lý do thất bại gần nhất để giao diện hiển thị đúng lỗi."""
    with _publish_error_lock:
        if message:
            _publish_errors[post_id] = message
        else:
            _publish_errors.pop(post_id, None)


def get_last_publish_error(post_id: int) -> str:
    """Lấy lý do đăng bài thất bại gần nhất theo Post ID."""
    with _publish_error_lock:
        return _publish_errors.get(post_id, "Không thể đăng bài lên UCircle.")


def _has_valid_image(post: dict) -> bool:
    """Chỉ đăng ảnh đã thẩm định và khớp đúng phiên bản bài hiện tại."""
    return current_image_is_approved(post, get_circle_config(post.get("circle_id")) or {})

CONFIG_PATH = Path(__file__).parent.parent / "config" / "circles.json"
SESSION_FILE = Path(__file__).parent.parent / "session.json"
AUTOMATION_CONFIG_FILE = Path(__file__).parent.parent / "config" / "automation.json"
# Thử kiểm tra session từ các tool lân cận nếu có
ALTERNATIVE_SESSION = Path(r"D:\Auto_tool\Fb-To-Ucricle\session.json")


def _publish_browser_headless() -> bool:
    """Đọc mới mỗi lượt để thay đổi trên Dashboard có hiệu lực ngay."""
    try:
        with open(AUTOMATION_CONFIG_FILE, encoding="utf-8") as file:
            browser_config = json.load(file).get("browser", {})
        if "publish_headless" in browser_config:
            return bool(browser_config.get("publish_headless"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Không đọc được cấu hình trình duyệt; dùng PLAYWRIGHT_HEADLESS.")
    return os.getenv("PLAYWRIGHT_HEADLESS", "false").strip().casefold() == "true"


def _get_circle_config(circle_id: str) -> dict:
    return get_circle_config(circle_id)


async def _new_publish_context(browser, context_args: dict):
    """Đọc snapshot session theo lượt để nhiều Circle không cùng đọc file đang ghi."""
    with _session_state_lock:
        return await browser.new_context(**context_args)


async def _save_publish_session(context) -> None:
    """Ghi session tuần tự để các tiến trình đăng song song không làm hỏng JSON."""
    with _session_state_lock:
        await context.storage_state(path=str(SESSION_FILE))


def _get_circle_target_url(circle_id: str) -> str:
    """Lấy target URL của circle từ cấu hình."""
    circle = _get_circle_config(circle_id)
    url = circle.get("ucircle_api_url") or circle.get("ucircle_circle_id")
    if url and url.startswith("http"):
        return url
    return "https://ucircle.net/app"


def _verification_snippet(text: str) -> str:
    """Lấy một đoạn tiêu đề ổn định để xác minh bài đã xuất hiện trên Circle."""
    for line in str(text or "").splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if len(normalized) >= 20 and not normalized.startswith(("📌", "🔗", "📰", "🖼", "#")):
            return normalized[:80]
    return re.sub(r"\s+", " ", str(text or "")).strip()[:80]


def _normalize_verification_text(value: str) -> str:
    """Chuẩn hóa văn bản để không trượt vì emoji, dấu câu hoặc khoảng trắng của React."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = value.replace("\u200b", " ").replace("\ufeff", " ")
    return re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).strip()


def _verification_needles(text: str) -> list[str]:
    """Tạo các khóa đủ dài để xác minh đúng bài nhưng chịu được UI cắt tiêu đề."""
    snippet = _normalize_verification_text(_verification_snippet(text))
    if not snippet:
        return []
    words = snippet.split()
    needles = [snippet]
    if len(words) >= 7:
        needles.append(" ".join(words[:7]))
    return list(dict.fromkeys(item for item in needles if len(item) >= 24))


async def _page_contains_published_post(page, text: str) -> bool:
    """Kiểm tra bài trên feed bằng tiêu đề đã chuẩn hóa."""
    needles = _verification_needles(text)
    if not needles:
        return False
    try:
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(1200)
        body_text = _normalize_verification_text(await page.locator("body").inner_text())
    except Exception:
        return False
    return any(needle in body_text for needle in needles)


async def _publish_via_playwright(circle_url: str, text: str, image_path: str = None, post_id: int = None) -> tuple[bool, str, str]:
    """
    Publish bài lên UCircle qua Playwright browser automation.
    Returns: (success: bool, platform_post_id: str, error_msg: str)
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return False, "", "Thư viện 'playwright' chưa được cài đặt trong môi trường Python"

    session_path = None
    if SESSION_FILE.exists():
        session_path = str(SESSION_FILE)
    elif ALTERNATIVE_SESSION.exists():
        session_path = str(ALTERNATIVE_SESSION)

    headless = _publish_browser_headless()

    if not session_path and headless:
        return False, "", "Chưa có session.json. Hãy chạy login_ucircle.bat trước khi đăng bài."

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--start-maximized"]
            )
            context_args = (
                {"viewport": {"width": 1440, "height": 1200}}
                if headless else {"no_viewport": True}
            )
            if session_path:
                context_args["storage_state"] = session_path

            context = await _new_publish_context(browser, context_args)
            page = await context.new_page()

            logger.info(f"🌐 Đang mở UCircle: {circle_url}")
            # Không đợi ảnh/analytics/iframe của trang; UCircle là SPA và
            # các tài nguyên phụ có thể khiến sự kiện `load` treo quá lâu.
            await page.goto(circle_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # Kiểm tra nếu chưa đăng nhập
            if "login" in page.url or "sso" in page.url:
                if headless:
                    await browser.close()
                    return False, "", "Phiên đăng nhập UCircle đã hết hạn. Hãy chạy lại login_ucircle.bat."
                logger.warning("⚠️ Cần đăng nhập UCircle trên trình duyệt. Vui lòng đăng nhập...")
                try:
                    await page.wait_for_url("**/app/**", timeout=60000)
                    # Lưu lại session
                    await _save_publish_session(context)
                    logger.info("✅ Đã lưu phiên đăng nhập vào session.json")
                    await page.goto(circle_url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(3000)
                except Exception as e:
                    await browser.close()
                    return False, "", f"Hết thời gian chờ đăng nhập: {e}"

            # Mở popup đăng bài
            create_btn = page.locator('span[class*="_composePlus"], button[aria-label*="Tạo bài viết"], div[data-placeholder*="nghĩ gì"], button:has-text("Tạo bài viết")')
            try:
                await create_btn.first.wait_for(state="visible", timeout=30000)
            except Exception:
                await browser.close()
                return False, "", "Trang UCircle đã mở nhưng không thấy nút soạn bài trong 30 giây; kiểm tra phiên đăng nhập hoặc giao diện Circle."

            await create_btn.first.click()

            # Chờ modal soạn thảo
            modal = page.locator('div[data-composer="true"]').first
            if await modal.count() == 0:
                modal = page.locator('div[role="dialog"]:has(textarea[data-composer-text="true"]), div[role="dialog"]:has(div[contenteditable="true"])').first
            await modal.wait_for(state="visible", timeout=8000)

            # ─── BƯỚC 1: CHỌN CHÍNH XÁC TƯ CÁCH ĐĂNG LÀ CIRCLE / TRANG ───
            try:
                circle_radio = modal.locator('input[data-composer-as="circle"]').first
                try:
                    await circle_radio.wait_for(state="attached", timeout=4000)
                except Exception:
                    pass

                circle_label = circle_radio.locator("xpath=ancestor::label[1]")
                identity_name = "Circle / Trang"
                if await circle_label.count() > 0:
                    identity_name = " ".join((await circle_label.inner_text()).split())

                if await circle_radio.count() > 0:
                    if not await circle_radio.is_checked():
                        if await circle_label.count() > 0:
                            await circle_label.click(timeout=3000)
                        else:
                            await circle_radio.check(timeout=3000)

                    # Chờ React cập nhật trạng thái radio checked
                    try:
                        await page.wait_for_function(
                            """() => {
                                const radio = document.querySelector('input[data-composer-as="circle"]');
                                return Boolean(radio && radio.checked);
                            }""",
                            timeout=4000,
                        )
                    except Exception:
                        pass

                    if await circle_radio.is_checked():
                        logger.info(f"  [+] ĐÃ CHỌN TƯ CÁCH ĐĂNG: '{identity_name}' (Circle - Xác nhận thành công)")
                    else:
                        logger.warning("  [*] Chưa kích hoạt được radio Circle, thử fallback click...")
                else:
                    # Fallback quét fieldset
                    await page.evaluate("""() => {
                        let allFieldsets = Array.from(document.querySelectorAll('fieldset'));
                        for (let fs of allFieldsets) {
                            let legend = fs.querySelector('legend');
                            if ((legend && legend.innerText.includes('tư cách')) || fs.innerText.includes('Đăng với tư cách')) {
                                let labels = Array.from(fs.querySelectorAll('label'));
                                for (let lbl of labels) {
                                    if (!lbl.innerText.includes('Cá nhân') && !lbl.innerText.includes('user')) {
                                        lbl.click();
                                        return;
                                    }
                                }
                            }
                        }
                    }""")
                    logger.info("  [+] Đã kích hoạt tư cách Circle qua fallback fieldset")
            except Exception as e_id:
                logger.warning(f"  [!] Lưu ý khi chuyển tư cách Circle: {e_id}")

            await page.wait_for_timeout(1000)

            # ─── BƯỚC 2: NHẬP NỘI DUNG VÀO EDITOR ───
            editor = modal.locator('textarea[data-composer-text="true"], div[contenteditable="true"]').first
            if await editor.count() > 0:
                await editor.click()
                await editor.fill(text)
                # Đảm bảo React nhận event input và change
                await page.evaluate("""(txt) => {
                    let el = document.querySelector('textarea[data-composer-text="true"], div[contenteditable="true"]');
                    if (el) {
                        if (el.tagName === 'TEXTAREA') {
                            el.value = txt;
                        }
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""", text)
                logger.info("  [+] Đã nhập nội dung bài viết vào editor")
                await page.wait_for_timeout(1000)

            # ─── BƯỚC 3: ĐÍNH KÈM NHIỀU ẢNH (TỐI ĐA 3) & CHỜ UPLOAD ───
            # Lấy tất cả ảnh của bài (tối đa 3)
            all_assets = []
            if post_id:
                try:
                    all_assets = get_all_media_assets_by_post(post_id)
                except Exception as e_assets:
                    logger.warning(f"⚠️ Không thể lấy media assets cho Post #{post_id}: {e_assets}")

            images_to_attach = []
            if all_assets:
                for asset in all_assets[:3]:
                    fp = asset.get("file_path") or ""
                    fn = asset.get("file_name") or ""
                    if fp and os.path.exists(fp):
                        images_to_attach.append(os.path.abspath(fp))
                    elif fn:
                        alt = str(Path(__file__).parent.parent / "data" / "images" / fn)
                        if os.path.exists(alt):
                            images_to_attach.append(os.path.abspath(alt))
            elif image_path and os.path.exists(image_path):
                images_to_attach = [os.path.abspath(image_path)]

            if images_to_attach:
                logger.info(f"📎 Đính kèm {len(images_to_attach)} ảnh vào bài viết...")
                file_input = modal.locator('input[data-composer-file="true"], input[type="file"][accept*="image"]').first
                add_btn = modal.locator('button[data-composer-addphoto="true"]').first

                attached = False
                try:
                    if await file_input.count() > 0:
                        # Thử truyền toàn bộ danh sách ảnh cùng lúc nếu input hỗ trợ multiple
                        await file_input.set_input_files(images_to_attach)
                        logger.info(f"  [+] Đã đính kèm {len(images_to_attach)} ảnh vào file input")
                        attached = True
                except Exception as e_batch:
                    logger.warning(f"⚠️ Không đính kèm cùng lúc được ({e_batch}), thử từng ảnh...")

                if not attached:
                    for i, abs_img in enumerate(images_to_attach):
                        try:
                            if await file_input.count() > 0:
                                await file_input.set_input_files(abs_img)
                                logger.info(f"  [+] Ảnh {i+1}: {os.path.basename(abs_img)}")
                            elif await add_btn.count() > 0:
                                async with page.expect_file_chooser(timeout=5000) as chooser_info:
                                    await add_btn.click()
                                chooser = await chooser_info.value
                                await chooser.set_files(abs_img)
                                logger.info(f"  [+] Ảnh {i+1} qua chooser: {os.path.basename(abs_img)}")
                            await page.wait_for_timeout(1500)
                        except Exception as e_img:
                            logger.warning(f"⚠️ Không đính kèm được ảnh {i+1}: {e_img}")

                # Chờ preview ảnh tải xong
                preview_confirmed = False
                try:
                    preview = modal.locator('[data-composer-prev="true"]').first
                    await preview.wait_for(state="visible", timeout=10000)
                    logger.info("  [+] Ảnh đã hiển thị preview hoàn tất.")
                    preview_confirmed = True
                except Exception:
                    await page.wait_for_timeout(3000)
                if not preview_confirmed:
                    await browser.close()
                    return False, "", "UCircle chưa hiển thị preview ảnh; hủy đăng để tránh bài không có ảnh."
            else:
                await browser.close()
                return False, "", "Không tìm thấy file ảnh hợp lệ để đính kèm; bài bị chặn đăng."


            # ─── BƯỚC 4: CHỜ NÚT ĐĂNG ĐƯỢC KÍCH HOẠT (ENABLED) & SUBMIT ───
            submit_btn = modal.locator('button[data-composer-post="true"]').first
            if await submit_btn.count() == 0:
                submit_btn = modal.get_by_role("button", name="Đăng", exact=True).first
            if await submit_btn.count() == 0:
                submit_btn = modal.locator('button:has-text("Đăng")').first

            if await submit_btn.count() == 0:
                raise RuntimeError("Không tìm thấy nút 'Đăng' trên giao diện UCircle")

            await submit_btn.wait_for(state="visible", timeout=6000)

            # Kiểm tra trạng thái enabled của nút Đăng
            enabled = False
            for attempt in range(30):
                if not await submit_btn.is_disabled():
                    enabled = True
                    break
                await page.wait_for_timeout(500)

            if not enabled:
                logger.warning("⚠️ Nút Đăng đang bị vô hiệu hóa, kích hoạt lại sự kiện nhập liệu...")
                await page.evaluate("""() => {
                    let el = document.querySelector('textarea[data-composer-text="true"], div[contenteditable="true"]');
                    if (el) {
                        el.focus();
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""")
                await page.wait_for_timeout(1500)
                enabled = not await submit_btn.is_disabled()

            if not enabled:
                await browser.close()
                return False, "", "Nút Đăng vẫn bị vô hiệu hóa sau khi nhập nội dung và tải ảnh"

            logger.info("🚀 Bấm nút Đăng bài...")
            try:
                await submit_btn.click(timeout=5000)
            except Exception as exc:
                await browser.close()
                return False, "", f"Không bấm được nút Đăng: {exc}"

            # Chờ popup đóng lại (xác nhận đăng bài thành công)
            try:
                await modal.wait_for(state="hidden", timeout=15000)
                logger.info("Popup soạn thảo đã đóng; đang xác minh bài trên Circle...")
            except Exception:
                await browser.close()
                return False, "", "Không nhận được xác nhận đăng thành công từ UCircle; popup vẫn đang mở"

            # Popup đóng chưa đủ chứng minh bài đã được lưu. Tải lại Circle và
            # chỉ báo thành công khi nội dung vừa đăng thật sự xuất hiện.
            verified = False
            # Kiểm tra ngay trên SPA trước; nếu feed chưa cập nhật thì tải lại với
            # thời gian chờ tăng dần. UCircle đôi khi cần hơn 10 giây mới đồng bộ.
            if await _page_contains_published_post(page, text):
                verified = True
            for verification_attempt in range(5):
                if verified:
                    break
                await page.wait_for_timeout(2500 + verification_attempt * 1500)
                await page.goto(circle_url, wait_until="domcontentloaded", timeout=30000)
                if "login" in page.url or "sso" in page.url:
                    await browser.close()
                    return False, "", "Phiên UCircle hết hạn trong lúc xác minh bài đăng."
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await page.wait_for_timeout(1800)
                if await _page_contains_published_post(page, text):
                    verified = True
                    break

            if not verified:
                await _save_publish_session(context)
                await browser.close()
                return (
                    False,
                    "",
                    "UCircle đã đóng cửa sổ soạn thảo nhưng chưa tìm thấy bài trên Circle đích; "
                    "hệ thống giữ bài ở trạng thái Chờ đăng để tránh báo thành công giả.",
                )

            # Lưu lại trạng thái phiên mới
            await _save_publish_session(context)
            await browser.close()

            platform_id = f"verified_{post_id or 'post'}_{int(time.time())}"
            return True, platform_id, ""

    except Exception as e:
        logger.error(f"❌ Playwright publish lỗi: {e}")
        return False, "", str(e)


def _run_async(coro):
    """Chạy coroutine an toàn kể cả khi đang ở trong Event Loop (FastAPI) hay không."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def publish_post(post_id: int, max_retries: int = 3, manual_override: bool = False) -> bool:
    """
    Thực hiện xuất bản bài viết lên UCircle với cơ chế Retry.

    manual_override chỉ bỏ qua khoảng nghỉ tối thiểu giữa hai bài.
    Giới hạn số bài theo giờ và ngày vẫn được áp dụng.
    """
    _set_publish_error(post_id, None)
    post = get_post_by_id(post_id)
    if not post:
        message = f"Không tìm thấy bài #{post_id}."
        _set_publish_error(post_id, message)
        logger.error(f"❌ {message}")
        return False

    if post.get("status") == "PUBLISHED":
        logger.info(f"Post #{post_id} đã được đăng trước đó, không đăng lặp lại.")
        return True
    if post.get("status") != "READY":
        message = f"Bài #{post_id} đang ở trạng thái {post.get('status')}; hãy duyệt bài trước khi đăng."
        _set_publish_error(post_id, message)
        logger.warning(message)
        return False
    if not _has_valid_image(post):
        message = f"Bài #{post_id} chưa có ảnh hợp lệ hoặc chưa được xác minh với nội dung hiện tại; hãy tiếp tục kiểm tra ảnh."
        _set_publish_error(post_id, message)
        update_post_status(post_id, "WAITING_IMAGE", reject_reason=message)
        logger.warning(message)
        return False

    circle_id = post.get("circle_id", "circle_tintuc24h")
    circle_config = _get_circle_config(circle_id)
    if not circle_config:
        message = f"Circle '{circle_id}' chưa có trong cấu hình."
        _set_publish_error(post_id, message)
        logger.warning(message)
        return False
    if not bool(circle_config.get("publish_enabled", circle_config.get("enabled", False))):
        message = (
            f"Circle '{circle_config.get('circle_name', circle_id)}' chưa bật tùy chọn Đăng bài "
            "trên dashboard."
        )
        _set_publish_error(post_id, message)
        logger.warning(message)
        return False

    allowed, reason = can_publish_now(circle_id, enforce_min_interval=not manual_override)
    if not allowed:
        _set_publish_error(post_id, reason)
        logger.warning(f"Chưa thể đăng Post #{post_id}: {reason}")
        return False

    if not claim_post_for_publish(post_id):
        message = f"Bài #{post_id} đang được tiến trình khác đăng hoặc không còn sẵn sàng."
        _set_publish_error(post_id, message)
        logger.warning(message)
        return False

    content = post.get("full_content") or post.get("ai_summary", "")
    image_path = post.get("image_path")
    target_url = _get_circle_target_url(circle_id)

    try:
        with open(Path(__file__).parent.parent / "config" / "automation.json", encoding="utf-8") as f:
            publish_config = json.load(f).get("publish_schedule", {})
        retry_delays = [
            min(300, max(1, int(v) * 60))
            for v in publish_config.get("retry_intervals_minutes", [1, 5, 15])
        ]
    except Exception:
        retry_delays = [60, 300, 300]

    max_retries = max(1, int(max_retries))
    error_msg = "Lỗi đăng bài không xác định"

    for attempt in range(max_retries):
        logger.info(f"🚀 Đang đăng bài #{post_id} lên UCircle (Lần thử {attempt + 1}/{max_retries})...")
        
        # Chạy async Playwright an toàn
        success, platform_post_id, error_msg = _run_async(
            _publish_via_playwright(target_url, content, image_path, post_id=post_id)
        )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if success:
            _set_publish_error(post_id, None)
            logger.info(f"🎉 Xuất bản thành công Post #{post_id} -> ID: {platform_post_id}")
            update_post_status(post_id, "PUBLISHED")
            save_publish_log({
                "post_id": post_id,
                "circle_id": circle_id,
                "platform": "ucircle",
                "publish_status": "SUCCESS",
                "platform_post_id": platform_post_id,
                "retry_count": attempt,
                "error_message": "",
                "published_at": now_str
            })
            return True
        else:
            logger.warning(f"⚠️ Lần thử {attempt + 1} thất bại: {error_msg}")
            if attempt < max_retries - 1:
                delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                logger.info(f"⏳ Chờ {delay} giây trước khi thử lại...")
                time.sleep(delay)

    # Thất bại sau các lần thử
    logger.error(f"❌ Không thể đăng bài #{post_id} sau {max_retries} lần thử.")
    _set_publish_error(post_id, error_msg)
    # Lỗi kỹ thuật không làm mất trạng thái duyệt; giữ READY để người dùng thử lại.
    update_post_status(post_id, "READY", reject_reason=error_msg)
    save_publish_log({
        "post_id": post_id,
        "circle_id": circle_id,
        "platform": "ucircle",
        "publish_status": "FAILED",
        "platform_post_id": None,
        "retry_count": max_retries,
        "error_message": error_msg,
        "published_at": None
    })
    return False
