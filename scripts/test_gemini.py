"""
scripts/test_gemini.py
Kiểm tra kết nối và hạn mức của GEMINI_API_KEY
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

load_dotenv()
from src.logger import get_logger
from src.gemini_client import generate_json

logger = get_logger("test_gemini")

def main():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("❌ Chưa tìm thấy GEMINI_API_KEY trong file .env!")
        logger.info("👉 Hãy tạo file .env từ .env.example và điền key của bạn.")
        return

    logger.info("🤖 Đang kết nối tới Google Gemini API...")
    try:
        reply = generate_json(
            'Trả về JSON đúng dạng {"message": "một câu chào ngắn bằng tiếng Việt"}.',
            temperature=0,
            purpose="kiểm tra kết nối",
        ).get("message", "")

        logger.info("✅ Kết nối Gemini API THÀNH CÔNG!")
        logger.info(f"Phản hồi từ AI: {reply}")
    except Exception as e:
        logger.error(f"❌ Kết nối thất bại: {e}")

if __name__ == "__main__":
    main()
