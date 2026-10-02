"""
scripts/init_db.py
Khởi tạo cơ sở dữ liệu SQLite và nạp danh sách nguồn tin ban đầu từ config/sources.json
"""

import sys
import json
from pathlib import Path

# Đảm bảo import được module từ thư mục gốc
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.storage import init_database, upsert_source
from src.logger import get_logger

logger = get_logger("init_db")

def main():
    logger.info("🚀 Bắt đầu khởi tạo cơ sở dữ liệu...")
    init_database()

    sources_file = ROOT_DIR / "config" / "sources.json"
    if not sources_file.exists():
        logger.error(f"❌ Không tìm thấy file nguồn: {sources_file}")
        return

    with open(sources_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    sources = data.get("sources", [])
    logger.info(f"📥 Đang nạp {len(sources)} nguồn tin vào database...")

    for src in sources:
        upsert_source(src)
        logger.info(f"  ✔️ Đã nạp nguồn: {src.get('name')} ({src.get('domain')})")

    logger.info("✨ Khởi tạo database và nạp nguồn tin thành công!")

if __name__ == "__main__":
    main()
