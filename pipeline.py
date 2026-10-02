"""
pipeline.py
Pipeline Orchestrator — UCircle AI Content Automation
Điều phối luồng xử lý toàn diện từ thu thập RSS đến xuất bản
"""

import os
import sys
import argparse
import time
from pathlib import Path
from datetime import datetime

# Đảm bảo import được module
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.collector import collect_all
from src.normalizer import normalize_batch
from src.deduplicator import filter_duplicates
from src.storage import (
    save_article,
    update_article_status,
    save_story,
    save_post,
    save_run_log,
    init_database
)
from src.analyzer import analyze_article
from src.writer import write_post
from src.image_workflow import complete_post_image
from src.queue_manager import can_publish_now, get_next_ready_post
from src.publisher import publish_post
from src.logger import get_logger, RunStats

logger = get_logger("pipeline")


def run_pipeline(dry_run: bool = False, limit_articles: int = 5, mode: str = "manual_review"):
    """
    Chạy toàn bộ pipeline xử lý tin tức.
    """
    stats = RunStats()
    logger.info("==================================================")
    logger.info(f"🚀 BẮT ĐẦU PIPELINE — Mode: {mode} | Dry-run: {dry_run}")
    logger.info("==================================================")

    init_database()

    # ----------------------------------------------------
    # BƯỚC 1: THU THẬP TỪ RSS
    # ----------------------------------------------------
    raw_articles = collect_all()
    stats.collected = len(raw_articles)
    if not raw_articles:
        logger.info("ℹ️ Không tìm thấy bài viết mới nào từ các nguồn RSS.")
        stats.print_summary()
        save_run_log(stats.to_dict())
        return

    # ----------------------------------------------------
    # BƯỚC 2: CHUẨN HOÁ DỮ LIỆU & LỌC TIER 1
    # ----------------------------------------------------
    normalized_articles, skipped = normalize_batch(raw_articles)
    stats.filtered += skipped

    # ----------------------------------------------------
    # BƯỚC 3: PHÁT HIỆN TRÙNG LẶP (DEDUPLICATION)
    # ----------------------------------------------------
    unique_articles, dup_count = filter_duplicates(normalized_articles)
    stats.url_duplicate = dup_count

    # Lọc tiếp qua Keyword Filter cơ bản để tiết kiệm hạn mức Gemini API
    candidates = []
    for art in unique_articles:
        if art.get("_passes_keyword_filter", True):
            candidates.append(art)
        else:
            stats.filtered += 1

    logger.info(f"🎯 Sau khi lọc: còn {len(candidates)} bài ứng viên phù hợp.")

    # Giới hạn số lượng bài phân tích trong một lượt chạy
    process_list = candidates[:limit_articles]
    logger.info(f"⚡ Tiến hành xử lý AI cho {len(process_list)} bài đầu tiên...")

    # ----------------------------------------------------
    # BƯỚC 4: AI ANALYZER, WRITER, IMAGE & VALIDATOR
    # ----------------------------------------------------
    for idx, article in enumerate(process_list, 1):
        title = article.get("original_title", "")
        logger.info(f"\n--- [{idx}/{len(process_list)}] Đang xử lý: '{title[:60]}...' ---")

        # Lưu bài thô vào database
        article_id = save_article(article)
        if not article_id:
            logger.warning("  ⚠️ Không thể lưu bài viết gốc vào DB.")
            continue

        # 4.1. Phân tích AI
        stats.ai_analyzed += 1
        analysis = analyze_article(article)
        if not analysis:
            update_article_status(article_id, "FAILED_ANALYSIS")
            continue

        # Kiểm tra tiêu chí duyệt của Analyzer
        is_approved = analysis.get("publish", False) and analysis.get("overall_score", 0) >= 80
        if not is_approved:
            logger.info(f"  🚫 Từ chối đăng: Điểm {analysis.get('overall_score')} - Lý do: {analysis.get('reason')}")
            update_article_status(article_id, "REJECTED")
            stats.rejected += 1
            continue

        # 4.2. Lưu thông tin Story
        story_id = save_story({
            "topic": analysis.get("topic", "news"),
            "main_title": title,
            "key_facts": analysis.get("key_facts", []),
            "confidence_score": analysis.get("overall_score", 0),
            "sensitivity_level": analysis.get("sensitivity_level", "low"),
            "is_sensitive": analysis.get("is_sensitive", False),
            "status": "ANALYZED"
        })
        update_article_status(article_id, "PROCESSED", story_id=story_id)

        # 4.3. AI Writer — Viết bản tóm tắt mới
        circle_id = "circle_tintuc24h"
        post_data = write_post(article, analysis, circle_id=circle_id)
        if not post_data:
            logger.warning("  ⚠️ Writer không tạo được bài viết.")
            continue

        post_data["source_url"] = article.get("source_url", "")
        post_data["source_name"] = article.get("source_name", "")
        post_data["source_image_url"] = article.get("source_image_url", "")
        post_data["thumbnail_url"] = article.get("thumbnail_url", "")

        # 4.4. Lưu bản nháp trước để media_assets luôn tham chiếu đúng post_id
        post_record = {
            "story_id": story_id,
            "article_id": article_id,
            "circle_id": circle_id,
            "ai_title": post_data.get("ai_title"),
            "ai_summary": post_data.get("ai_summary"),
            "bullet_points": post_data.get("bullet_points", []),
            "hashtags": post_data.get("hashtags", []),
            "source_name": article.get("source_name"),
            "source_url": article.get("source_url"),
            "full_content": post_data.get("full_content"),
            "relevance_score": analysis.get("relevance_score", 0),
            "trust_score": analysis.get("trust_score", 0),
            "freshness_score": analysis.get("freshness_score", 0),
            "confidence_score": analysis.get("overall_score", 0),
            "ai_reason": analysis.get("reason"),
            "status": "DRAFT"
        }
        post_record["image_analysis"] = analysis
        post_id = save_post(post_record)

        # 4.5. Lấy ảnh nguồn hoặc tạo ảnh AI dự phòng
        image_result = complete_post_image(post_id)
        post_status = image_result.get("status", "WAITING_IMAGE")
        stats.draft_generated += 1

        if post_status == "REVIEW":
            stats.review_pending += 1
            logger.info(f"  👀 Bài #{post_id} cần kiểm duyệt thủ công (Điểm thẩm định: {image_result.get('score', 0)}).")
        elif post_status == "READY":
            logger.info(f"  ✅ Bài #{post_id} đã ĐẠT chuẩn và sẵn sàng xuất bản!")
        elif post_status == "WAITING_IMAGE":
            logger.warning(f"  🖼 Bài #{post_id} Chờ ảnh: {image_result.get('error', '')}")

        time.sleep(1)

    # ----------------------------------------------------
    # BƯỚC 5: PUBLISH QUEUE (NẾU BẬT TỰ ĐỘNG)
    # ----------------------------------------------------
    if mode in ["semi_auto", "full_auto"] and not dry_run:
        logger.info("\n📢 Đang kiểm tra Publish Queue...")
        circle_id = "circle_tintuc24h"
        allowed, reason = can_publish_now(circle_id)
        if allowed:
            next_post = get_next_ready_post(circle_id)
            if next_post:
                logger.info(f"🚀 Tiến hành xuất bản bài #{next_post['id']}...")
                success = publish_post(next_post["id"])
                if success:
                    stats.published += 1
                else:
                    stats.failed += 1
        else:
            logger.info(f"⏸ Tạm dừng xuất bản: {reason}")
    elif dry_run:
        logger.info("🧪 [Dry-run] Bỏ qua bước xuất bản lên UCircle.")

    # ----------------------------------------------------
    # BƯỚC 6: TỔNG KẾT VÀ GHI LOG
    # ----------------------------------------------------
    stats.print_summary()
    save_run_log(stats.to_dict())


def main():
    parser = argparse.ArgumentParser(description="UCircle AI Content Automation Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Chạy thử nghiệm không publish")
    parser.add_argument("--limit", type=int, default=3, help="Số bài tối đa phân tích bằng Gemini")
    parser.add_argument("--mode", type=str, default="manual_review", choices=["manual_review", "semi_auto", "full_auto"], help="Chế độ vận hành")
    args = parser.parse_args()

    run_pipeline(dry_run=args.dry_run, limit_articles=args.limit, mode=args.mode)


if __name__ == "__main__":
    main()
