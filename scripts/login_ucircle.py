"""
scripts/login_ucircle.py
Cong cu dang nhap UCircle bang Gmail + OTP tren trinh duyet
Va tu dong luu session.json cho tat ca cac tool su dung
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
SESSION_FILE = ROOT_DIR / "session.json"
FB_SESSION_FILE = Path(r"D:\Auto_tool\Fb-To-Ucricle\session.json")


async def login():
    print("=" * 65)
    print("   ĐĂNG NHẬP UCIRCLE BẰNG GMAIL + OTP (LƯU PHIÊN SESSION.JSON)")
    print("=" * 65)
    print("\n🌐 Đang mở trình duyệt Chromium...")

    async with async_playwright() as p:
        browser = None
        try:
            browser = await p.chromium.launch(
                headless=False,
                args=["--start-maximized"]
            )
        except Exception as chromium_error:
            print(f"  [!] Không mở được Chromium: {chromium_error}")
            print("  [*] Đang thử mở Microsoft Edge...")
            try:
                browser = await p.chromium.launch(
                    channel="msedge",
                    headless=False,
                    args=["--start-maximized"]
                )
            except Exception as edge_error:
                print(f"\n❌ Không thể mở trình duyệt: {edge_error}")
                print("Hãy chạy: python -m playwright install chromium")
                return False

        context_args = {"no_viewport": True}
        if SESSION_FILE.exists():
            context_args["storage_state"] = str(SESSION_FILE)
            print("  [*] Đã tìm thấy session.json cũ, đang kiểm tra...")
        elif FB_SESSION_FILE.exists():
            context_args["storage_state"] = str(FB_SESSION_FILE)
            print("  [*] Đang mượn session.json từ Fb-To-Ucricle để kiểm tra...")

        context = await browser.new_context(**context_args)
        page = await context.new_page()

        print("  [*] Đang kết nối tới https://ucircle.net/app ...")
        await page.goto("https://ucircle.net/app", timeout=60000)
        await page.wait_for_timeout(3000)

        # Kiểm tra xem đã trong phiên đăng nhập chưa
        if "login" not in page.url and "sso" not in page.url and "/app" in page.url:
            print("\n🎉 BẠN ĐÃ ĐĂNG NHẬP SẴN TRÊN UCIRCLE!")
            print(f"  URL hiện tại: {page.url}")
        else:
            print("\n" + "!" * 65)
            print("👉 VUI LÒNG NHẬP GMAIL VÀ MÃ OTP TRÊN CỬA SỔ TRÌNH DUYỆT ĐỂ ĐĂNG NHẬP.")
            print("   Hệ thống đang chờ bạn đăng nhập xong...")
            print("!" * 65 + "\n")

            try:
                # UCircle có thể chuyển đến /app hoặc /app/..., chấp nhận cả hai.
                await page.wait_for_function(
                    "() => location.pathname === '/app' || location.pathname.startsWith('/app/')",
                    timeout=300000,
                )
                await page.wait_for_timeout(3000)
                print("\n🎉 ĐĂNG NHẬP THÀNH CÔNG!")
            except Exception as e:
                print(f"\n❌ Hết thời gian chờ đăng nhập (5 phút) hoặc lỗi: {e}")
                await browser.close()
                return False

        print("\nTrình duyệt sẽ được giữ mở để bạn kiểm tra tài khoản.")
        print("Nếu cần, hãy đăng xuất/đăng nhập lại ngay trên cửa sổ này.")
        await asyncio.to_thread(
            input,
            "\n>>> Sau khi đã vào được UCircle, nhấn ENTER tại đây để lưu phiên: ",
        )

        if page.is_closed():
            print("\n❌ Cửa sổ trình duyệt đã bị đóng trước khi lưu phiên.")
            await browser.close()
            return False

        if not (page.url.rstrip("/").endswith("/app") or "/app/" in page.url):
            print(f"\n❌ Chưa xác nhận đăng nhập UCircle. URL hiện tại: {page.url}")
            print("Vui lòng chạy lại file và chỉ nhấn ENTER sau khi đã vào trang /app.")
            await browser.close()
            return False

        # Lưu session vào thư mục hiện tại
        await context.storage_state(path=str(SESSION_FILE))
        print(f"✅ Đã lưu phiên đăng nhập thành công vào: {SESSION_FILE}")

        # Đồng bộ ngược lại sang Fb-To-Ucricle nếu thư mục tồn tại
        if FB_SESSION_FILE.parent.exists():
            try:
                await context.storage_state(path=str(FB_SESSION_FILE))
                print(f"✅ Đã đồng bộ session sang: {FB_SESSION_FILE}")
            except Exception:
                pass

        await browser.close()
        print("\nHoàn tất! Bây giờ các tiến trình đăng bài tự động có thể chạy mà không cần hỏi lại.")
        return True


if __name__ == "__main__":
    try:
        success = asyncio.run(login())
    except KeyboardInterrupt:
        print("\n⚠️ Đã hủy thao tác đăng nhập.")
        success = False
    except Exception as exc:
        print(f"\n❌ Lỗi không mong muốn khi đăng nhập UCircle: {exc}")
        success = False

    raise SystemExit(0 if success else 1)
