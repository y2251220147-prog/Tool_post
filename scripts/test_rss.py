"""
scripts/test_rss.py
Kiểm tra khả năng đọc và cấu trúc dữ liệu trả về từ tất cả các RSS Feed đã cấu hình
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.storage import init_database, upsert_source, get_enabled_sources
from src.collector import collect_all
from src.normalizer import normalize_batch
from src.logger import get_logger

logger = get_logger("test_rss")

def main():
    logger.info("📡 Bắt đầu kiểm tra kết nối các nguồn RSS...")

    # Nạp DB
    init_database()
    import json
    with open(ROOT_DIR / "config" / "sources.json", encoding="utf-8") as f:
        sources = json.load(f).get("sources", [])
        for s in sources:
            upsert_source(s)

    raw_articles = collect_all()
    logger.info(f"✅ Thu thập thành công {len(raw_articles)} bài viết thô.")

    if raw_articles:
        logger.info("\n--- Xem thử 1 bài viết thô đầu tiên ---")
        sample = raw_articles[0]
        logger.info(f"Nguồn: {sample.get('source_name')}")
        logger.info(f"Tiêu đề: {sample.get('original_title')}")
        logger.info(f"URL: {sample.get('source_url')}")
        logger.info(f"Ngày đăng: {sample.get('published_at')}")

        normalized, skipped = normalize_batch(raw_articles[:10])
        logger.info(f"\n--- Chuẩn hoá thử 10 bài: {len(normalized)} bài đạt, {skipped} bài bị loại ---")
        if normalized:
            logger.info(f"Tiêu đề chuẩn hoá: {normalized[0].get('original_title')}")
            logger.info(f"Lọc từ khoá: {normalized[0].get('_passes_keyword_filter')}")
            logger.info(f"Mức nhạy cảm: {normalized[0].get('_sensitivity_level')}")

    logger.info("\n🎉 Kiểm tra RSS hoàn tất thành công!")

if __name__ == "__main__":
    main()
