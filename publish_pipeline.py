"""
publish_pipeline.py
PHASE 2 — Publish Pipeline (chay theo lich dang bai)

Luong xu ly:
  Chon bai tot nhat tu hang doi
  → Gemini Analyzer (phan tich & cham diem)
  → Gemini Writer (viet ban tom tat moi)
  → Gemini Image Generator (tao prompt + anh minh hoa)
  → Content Validator (kiem duyet 12 tieu chi)
  → Luu DB (READY / REVIEW / REJECTED)
  → [neu READY + auto mode] UCircle Publisher
  → Luu Publish Log
  → Cho slot tiep theo
"""

import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.storage import (
    init_database,
    get_best_queued_article,
    get_queued_article_count,
    update_article_status,
    save_story,
    save_post,
    save_publish_log,
    get_queue_stats,
    save_run_log,
    count_published_today,
    count_published_this_hour,
    get_posts_by_status,
    claim_article_for_processing,
    recover_stale_processing_articles,
)
from src.analyzer import analyze_article
from src.writer import write_post
from src.image_workflow import complete_post_image
from src.publisher import publish_post
from src.logger import get_logger, RunStats

logger = get_logger("publish")


def _load_config() -> dict:
    config_path = ROOT_DIR / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _is_within_active_hours(config: dict) -> bool:
    """Kiem tra xem hien tai co trong khung gio hoat dong khong."""
    sched = config.get("publish_schedule", {})
    start = sched.get("active_hours_start", 6)
    end = sched.get("active_hours_end", 23)
    current_hour = datetime.now().hour
    return start <= current_hour < end


def _can_publish(circle_id: str, config: dict) -> tuple[bool, str]:
    """Kiem tra rate limit truoc khi dang bai."""
    sched = config.get("publish_schedule", {})
    max_day = sched.get("max_posts_per_day", 20)
    max_hour = sched.get("max_posts_per_hour", 4)

    count_day = count_published_today(circle_id)
    if max_day is not None and int(max_day) > 0 and count_day >= int(max_day):
        return False, f"Da dat gioi han {count_day}/{max_day} bai/ngay"

    count_hour = count_published_this_hour(circle_id)
    if count_hour >= max_hour:
        return False, f"Da dat gioi han {count_hour}/{max_hour} bai/gio"

    return True, "OK"


def run_publish(dry_run: bool = False, circle_id: str = "circle_tintuc24h"):
    """
    Chay Phase 2: Chon bai tot nhat tu hang doi va xu ly AI + Publish.
    """
    stats = RunStats()
    config = _load_config()
    sched = config.get("publish_schedule", {})
    ai_cfg = config.get("ai", {})
    queue_cfg = config.get("queue", {})
    expire_hours = queue_cfg.get("article_expire_hours", 48)
    min_score = ai_cfg.get("min_publish_score", 82)
    operation_mode = config.get("operation_mode", "manual_review")

    logger.info("=" * 55)
    logger.info(f"[PHASE 2] BAT DAU PUBLISH - {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    logger.info(f"  Mode: {operation_mode} | Dry-run: {dry_run}")
    logger.info("=" * 55)

    init_database()
    recovered = recover_stale_processing_articles(stale_minutes=30)
    if recovered:
        logger.warning(f"  Đã khôi phục {recovered} bài bị kẹt PROCESSING về QUEUED.")

    # --- Kiem tra khung gio hoat dong ---
    if not _is_within_active_hours(config):
        start_h = sched.get("active_hours_start", 6)
        end_h = sched.get("active_hours_end", 23)
        logger.info(f"  Ngoai khung gio hoat dong ({start_h}h - {end_h}h). Bo qua.")
        return

    # --- Kiem tra rate limit ---
    allowed, reason = _can_publish(circle_id, config)
    if not allowed:
        logger.info(f"  Rate limit: {reason}. Bo qua.")
        return

    # --- KIỂM TRA ƯU TIÊN: NẾU ĐÃ CÓ BÀI DUYỆT (READY) ĐANG CHỜ ĐĂNG ---
    ready_posts = get_posts_by_status("READY", circle_id)
    if ready_posts and operation_mode in ["semi_auto", "full_auto"]:
        # Lấy bài READY cũ nhất đang chờ trong hàng đợi
        ready_post = ready_posts[-1]
        post_id = ready_post["id"]
        logger.info(f"🎯 Phát hiện bài #{post_id} đã duyệt (READY) đang chờ đăng: '{ready_post.get('ai_title')}'")
        if not dry_run:
            logger.info(f"🚀 Tiến hành đăng bài #{post_id} lên UCircle...")
            success = publish_post(post_id)
            if success:
                stats.published += 1
                logger.info("🎉 Đăng bài thành công!")
            else:
                stats.failed += 1
                logger.info("❌ Đăng bài thất bại. Xem logs để biết thêm.")
        else:
            logger.info(f"  [Dry-run] Bỏ qua bước đăng thật cho bài #{post_id}.")
        _log_run(stats, "publish_ready_post")
        return

    # --- Thong ke hang doi ---
    queue_count = get_queued_article_count()
    queue_stats = get_queue_stats()
    logger.info(f"  Hang doi: {queue_stats}")

    if queue_count == 0:
        logger.info("  Hang doi trong rong. Cho Phase 1 thu thap them.")
        return

    # --- Chon bai tot nhat ---
    article = get_best_queued_article(expire_hours=expire_hours)
    if not article:
        logger.info("  Khong tim duoc bai hop le trong hang doi.")
        return

    title = article.get("original_title", "")
    source_name = article.get("source_name", "")
    article_id = article.get("id")
    logger.info(f"  Bai duoc chon: '{title[:65]}...'")
    logger.info(f"  Nguon: {source_name} | Trust: {article.get('trust_level', '?')}")

    # Danh dau dang xu ly de tranh pick trung
    if not claim_article_for_processing(article_id):
        logger.info(f"  Bài #{article_id} đã được một tiến trình khác nhận xử lý. Bỏ qua.")
        return
    stats.ai_analyzed += 1

    # ─── BUOC 1: GEMINI ANALYZER ───────────────────────────────
    logger.info("  [1/4] Gemini phan tich bai...")
    analysis = analyze_article(article)

    if not analysis:
        logger.warning("  Gemini khong phan tich duoc. Tra bai ve QUEUED.")
        update_article_status(article_id, "QUEUED")
        return

    overall_score = analysis.get("overall_score", 0)
    should_publish = analysis.get("publish", False)

    logger.info(f"  Diem AI: {overall_score}/100 | Publish: {should_publish} | Ly do: {analysis.get('reason', '')[:60]}")

    if not should_publish or overall_score < min_score:
        logger.info(f"  Bai bi tu choi (diem {overall_score} < {min_score}). REJECTED.")
        update_article_status(article_id, "REJECTED")
        stats.rejected += 1
        _log_run(stats, "publish_phase")
        return

    # ─── BUOC 2: GEMINI WRITER ─────────────────────────────────
    logger.info("  [2/4] Gemini viet bai tom tat...")
    post_data = write_post(article, analysis, circle_id=circle_id)

    if not post_data:
        logger.warning("  Writer loi. Tra bai ve QUEUED de thu lai sau.")
        update_article_status(article_id, "QUEUED")
        return

    post_data["source_url"] = article.get("source_url", "")
    post_data["source_name"] = source_name or article.get("source_name", "")
    post_data["source_image_url"] = article.get("source_image_url", "")
    post_data["thumbnail_url"] = article.get("thumbnail_url", "")

    # Luu Story
    story_id = save_story({
        "topic": analysis.get("topic", "news"),
        "main_title": title,
        "key_facts": analysis.get("key_facts", []),
        "confidence_score": overall_score,
        "sensitivity_level": analysis.get("sensitivity_level", "low"),
        "is_sensitive": analysis.get("is_sensitive", False),
        "status": "ANALYZED"
    })
    update_article_status(article_id, "PROCESSED", story_id=story_id)

    # ─── LƯU GENERATED POST TRƯỚC (để có post_id cho media_assets) ───
    post_record = {
        "story_id": story_id,
        "article_id": article_id,
        "circle_id": circle_id,
        "ai_title": post_data.get("ai_title"),
        "ai_summary": post_data.get("ai_summary"),
        "bullet_points": post_data.get("bullet_points", []),
        "hashtags": post_data.get("hashtags", []),
        "source_name": source_name or article.get("source_name", ""),
        "source_url": article.get("source_url", ""),
        "full_content": post_data.get("full_content", ""),
        "relevance_score": analysis.get("relevance_score", 0),
        "trust_score": analysis.get("trust_score", 0),
        "freshness_score": analysis.get("freshness_score", 0),
        "confidence_score": overall_score,
        "ai_reason": analysis.get("reason", ""),
        "status": "DRAFT"
    }
    post_record["image_analysis"] = analysis
    post_id = save_post(post_record)

    # Dùng cùng luồng ảnh với Dashboard: ngữ cảnh đầy đủ và kiểm tra ảnh thật.
    logger.info("  [3/4] Lấy/tạo ảnh và kiểm tra với toàn bài...")
    image_result = complete_post_image(post_id)
    val_status = image_result.get("status", "WAITING_IMAGE")
    logger.info(f"  [4/4] Trạng thái: {val_status} | Điểm: {image_result.get('score', 0)}")
    if image_result.get("error"):
        logger.warning(f"    - {image_result['error']}")

    stats.draft_generated += 1

    if val_status == "REVIEW":
        stats.review_pending += 1
        logger.info(f"  Bai #{post_id} -> REVIEW (can duyet thu cong tai http://localhost:8000)")

    elif val_status == "REJECTED":
        stats.rejected += 1
        logger.info(f"  Bai #{post_id} -> REJECTED")

    elif val_status == "READY":
        logger.info(f"  Bai #{post_id} -> READY")

        # ─── PUBLISH ───────────────────────────────────────────
        if operation_mode in ["semi_auto", "full_auto"] and not dry_run:
            logger.info(f"  Dang bai #{post_id} len UCircle...")
            success = publish_post(post_id)
            if success:
                stats.published += 1
                logger.info(f"  Dang bai thanh cong!")
            else:
                stats.failed += 1
                logger.info(f"  Dang bai that bai. Xem logs de biet them.")
        elif dry_run:
            logger.info(f"  [Dry-run] Bo qua buoc dang bai.")
        else:
            logger.info(f"  Mode '{operation_mode}': Bai cho duyet thu cong tai Review UI.")

    _log_run(stats, "publish_phase")

    logger.info("=" * 55)
    logger.info(f"[PHASE 2] HOAN THANH | Post #{post_id} | Status: {val_status}")
    logger.info("=" * 55)


def _log_run(stats: RunStats, notes: str = "publish_phase"):
    try:
        save_run_log({**stats.to_dict(), "notes": notes})
    except Exception:
        pass


# Alias for compatibility
run_publish_cycle = run_publish


def check_post_eligibility_cli(post_id: int):
    from review_ui.app import evaluate_post_eligibility
    from src.storage import get_post_by_id
    post = get_post_by_id(post_id)
    if not post:
        print(f"❌ Không tìm thấy bài viết #{post_id}")
        return
    res = evaluate_post_eligibility(post)
    print("=" * 65)
    print(f"📊 BÁO CÁO ĐIỀU KIỆN ĐĂNG BÀI #{post_id}")
    print(f"  Tiêu đề : {post.get('ai_title')}")
    print(f"  Trạng thái: {post.get('status')} | Điểm thẩm định: {res['score']}/100")
    print(f"  Kết luận  : {res['verdict_text']}")
    print(f"  Mô tả     : {res['verdict_desc']}")
    print("=" * 65)
    print("📋 Chi tiết Checklist:")
    for c in res["checklist"]:
        icon = "✅" if c["passed"] else ("❌" if c["severity"] == "critical" else "⚠️")
        print(f"  {icon} [{c['category']}] {c['title']}: {c['detail']}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="UCircle Publish Pipeline - Phase 2")
    parser.add_argument("--dry-run", action="store_true", help="Chay thu nghiem, khong dang bai that")
    parser.add_argument("--circle", type=str, default="circle_tintuc24h", help="Circle ID")
    parser.add_argument("--check", type=int, default=None, help="Kiem tra tong the va dieu kien dang cua Post ID")
    args = parser.parse_args()

    if args.check is not None:
        check_post_eligibility_cli(args.check)
    else:
        run_publish(dry_run=args.dry_run, circle_id=args.circle)


if __name__ == "__main__":
    main()
