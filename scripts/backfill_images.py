"""Bổ sung ảnh nguồn cho bài chưa đăng, bắt buộc thẩm định ảnh như Dashboard."""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.image_context import current_image_is_approved
from src.image_workflow import complete_post_image
from src.storage import get_connection, get_post_by_id, init_database
from src.ucircle_account import get_circle_config


def main():
    init_database()
    conn = get_connection()
    try:
        post_ids = [row[0] for row in conn.execute(
            "SELECT id FROM generated_posts WHERE status IN ('DRAFT', 'REVIEW', 'READY', 'WAITING_IMAGE') ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()

    updated = 0
    skipped = 0
    for post_id in post_ids:
        post = get_post_by_id(post_id)
        if not post:
            skipped += 1
            continue
        circle = get_circle_config(post.get("circle_id")) or {}
        if post.get("is_ai_generated") == 0 and current_image_is_approved(post, circle):
            skipped += 1
            continue

        # Khóa bài, lưu lịch sử ảnh mới và kiểm tra nội dung; không dùng lại PASS
        # của ảnh trước, không tạo AI hoặc đăng bài trong công cụ bổ sung ảnh nguồn.
        result = complete_post_image(post_id, force_source=True)
        if result.get("success"):
            updated += 1
            print(f"[+] Bài #{post_id}: ảnh nguồn đã được kiểm tra, bài Chờ đăng.")
        else:
            skipped += 1
            print(f"[-] Bài #{post_id}: {result.get('error') or 'Ảnh chưa đạt; bài chưa được đăng.'}")
    print(f"Hoàn tất: {updated} bài đã đạt; {skipped} bài bỏ qua hoặc cần tiếp tục kiểm tra ảnh.")


if __name__ == "__main__":
    main()
