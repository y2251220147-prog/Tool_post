"""Kiểm tra phiên UCircle và khả năng nhìn thấy nút tạo bài, không đăng nội dung."""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT_DIR / "session.json"
CIRCLES_CONFIG = ROOT_DIR / "config" / "circles.json"


def _circle_url() -> str:
    with open(CIRCLES_CONFIG, encoding="utf-8") as f:
        circles = json.load(f).get("circles", [])
    for circle in circles:
        if circle.get("circle_id") == "circle_tintuc24h":
            return circle.get("ucircle_circle_id") or "https://ucircle.net/app"
    return "https://ucircle.net/app"


async def main() -> int:
    if not SESSION_FILE.exists() or SESSION_FILE.stat().st_size < 100:
        print("SESSION_MISSING")
        return 1

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=str(SESSION_FILE))
        page = await context.new_page()
        try:
            await page.goto(_circle_url(), timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            if "login" in page.url or "sso" in page.url:
                print("SESSION_EXPIRED")
                return 1
            selector = 'span[class*="_composePlus"], button[aria-label*="Tạo bài viết"], div[data-placeholder*="nghĩ gì"], button:has-text("Tạo bài viết")'
            count = await page.locator(selector).count()
            print(f"SESSION_OK composer_controls={count}")
            return 0 if count > 0 else 1
        finally:
            await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
