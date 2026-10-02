"""Giải liên kết trung gian về đúng trang bài viết của nhà xuất bản."""

from functools import lru_cache
from urllib.parse import urlparse

from .logger import get_logger


logger = get_logger("source_resolver")


def is_google_news_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    return parsed.hostname == "news.google.com" and "/articles/" in parsed.path


@lru_cache(maxsize=512)
def resolve_source_url(url: str, timeout_seconds: float = 12) -> str:
    """Mở liên kết Google News bằng trình duyệt nền và trả về URL báo gốc.

    Google News hiện chuyển hướng bằng JavaScript nên httpx/requests không thể
    lấy URL cuối chỉ bằng ``follow_redirects=True``.
    """
    source_url = str(url or "").strip()
    if not is_google_news_url(source_url):
        return source_url

    timeout_ms = max(4_000, int(float(timeout_seconds) * 1_000))
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.route(
                    "**/*",
                    lambda route: route.abort()
                    if route.request.resource_type in {"image", "media", "font", "stylesheet"}
                    else route.continue_(),
                )
                page = context.new_page()
                page.goto(source_url, wait_until="commit", timeout=timeout_ms)
                page.wait_for_url(
                    lambda current: urlparse(current).hostname != "news.google.com",
                    timeout=timeout_ms,
                )
                resolved = str(page.url or "").strip()
            finally:
                browser.close()
    except Exception as exc:
        logger.warning(f"Không giải được liên kết Google News: {str(exc)[:180]}")
        return source_url

    if resolved.startswith(("http://", "https://")) and not is_google_news_url(resolved):
        logger.info(f"Đã giải liên kết Google News sang nguồn: {urlparse(resolved).hostname}")
        return resolved
    return source_url
