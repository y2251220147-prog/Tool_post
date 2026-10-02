"""
collection_pipeline.py
PHASE 1 — Collection Pipeline (chay moi 60 phut)

Luong xu ly:
  RSS Collector → Normalizer → Deduplicator → Loc HOT → Luu vao hang doi (status=QUEUED)

KHONG goi Gemini AI o buoc nay.
Muc tieu: thu thap nhanh, chi giu bai HOT nhat, tranh bai da dang.
"""

import sys
import json
import time
from pathlib import Path
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.collector import collect_from_source
from src.normalizer import normalize_batch
from src.deduplicator import filter_duplicates
from src.storage import (
    init_database,
    save_article,
    expire_old_queued_articles,
    get_queue_stats,
    save_run_log,
    get_enabled_sources,
    get_published_source_urls,
    sync_sources_from_config,
    recover_stale_processing_articles,
    enforce_queue_limit,
)
from src.logger import get_logger, RunStats

logger = get_logger("collection")


def _load_config() -> dict:
    config_path = ROOT_DIR / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _score_article(article: dict) -> float:
    """
    Tính điểm HOT cho bài viết dựa trên:
    - Độ uy tín nguồn (trust_level)
    - Độ tươi (thời gian đăng so với hiện tại)
    - Độ dài tiêu đề (bài hot thường có tiêu đề dài vừa phải)
    """
    trust = article.get("_trust_level", 80)

    # Tính độ tươi: bài càng mới điểm càng cao
    freshness = 0.5
    pub_str = article.get("published_at", "")
    if pub_str:
        try:
            pub_dt = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            hours_old = (now - pub_dt).total_seconds() / 3600
            # Bài < 2h: freshness = 1.0; bài 24h: freshness = 0.0
            freshness = max(0.0, min(1.0, 1.0 - hours_old / 24.0))
        except Exception:
            pass

    # Tiêu đề dài vừa (40-120 ký tự) được đánh giá tốt hơn
    title_len = len(article.get("original_title", ""))
    title_bonus = 0.1 if 40 <= title_len <= 120 else 0.0

    # Mô tả có nội dung thêm điểm
    desc_bonus = 0.05 if len(article.get("original_description", "")) > 50 else 0.0

    score = trust * (0.5 + 0.45 * freshness + title_bonus + desc_bonus)
    return round(max(0.0, min(100.0, score)), 2)


def _article_age_hours(article: dict) -> float | None:
    """Tính tuổi bài theo giờ; trả về None nếu RSS không có ngày hợp lệ."""
    published_at = article.get("published_at", "")
    if not published_at:
        return None
    try:
        published = datetime.strptime(published_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def run_collection():
    """
    Chay Phase 1: Thu thap + Loc HOT + Luu hang doi.
    - Chi giu top N bai HOT nhat tu moi nguon
    - Khong dang bai da duoc PUBLISHED truoc do
    - Khong goi AI
    """
    stats = RunStats()
    config = _load_config()
    collection_cfg = config.get("collection", {})
    expire_hours = collection_cfg.get("article_max_age_hours", 24)
    max_per_run = collection_cfg.get("max_articles_per_run", 20)
    top_per_source = collection_cfg.get("top_articles_per_source", 3)
    min_trust = collection_cfg.get("min_trust_level", 80)

    logger.info("=" * 55)
    logger.info(f"[PHASE 1] BAT DAU THU THAP - {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    logger.info(f"  Config: Top {top_per_source}/nguon | Max {max_per_run} bai/lan | Min trust {min_trust}")
    logger.info("=" * 55)

    init_database()
    synced_sources = sync_sources_from_config()
    recovered = recover_stale_processing_articles(stale_minutes=30)
    if recovered:
        logger.warning(f"  Đã khôi phục {recovered} bài bị kẹt PROCESSING về QUEUED.")
    logger.info(f"  Đã đồng bộ {synced_sources} nguồn RSS từ cấu hình.")

    # --- Buoc 1: Expire cac bai cu trong hang doi ---
    expired = expire_old_queued_articles(expire_hours=expire_hours)
    if expired > 0:
        logger.info(f"  Xoa {expired} bai da qua han khoi hang doi.")

    # --- Buoc 2: Lay danh sach URL da duoc PUBLISHED de tranh dang lai ---
    published_urls = get_published_source_urls()
    logger.info(f"  Da co {len(published_urls)} bai duoc PUBLISHED (se loai bo).")

    # --- Buoc 3: Thu thap RSS tu tung nguon, loc HOT ngay ---
    sources = get_enabled_sources()
    if not sources:
        logger.warning("  Khong co source nao duoc bat!")
        _log_run(stats)
        return

    logger.info(f"  Thu thap tu {len(sources)} nguon RSS...")
    hot_articles_all = []

    for source in sources:
        trust_level = source.get("trust_level", 80)
        if trust_level < min_trust:
            logger.info(f"  Bo qua {source['name']} (trust {trust_level} < {min_trust})")
            continue

        raw_articles = collect_from_source(source)
        stats.collected += len(raw_articles)

        if not raw_articles:
            continue

        # Lọc bài quá cũ trước khi xếp hạng để tránh RSS dài lấn át tin mới.
        fresh_articles = []
        for raw_article in raw_articles:
            age_hours = _article_age_hours(raw_article)
            if age_hours is None or age_hours <= expire_hours:
                fresh_articles.append(raw_article)
        stale_count = len(raw_articles) - len(fresh_articles)
        raw_articles = fresh_articles
        stats.filtered += stale_count
        if stale_count:
            logger.info(f"    Loại bỏ {stale_count} bài cũ hơn {expire_hours} giờ")

        # Loc bai da duoc PUBLISHED
        before_filter = len(raw_articles)
        raw_articles = [a for a in raw_articles if a.get("source_url") not in published_urls]
        filtered_published = before_filter - len(raw_articles)
        if filtered_published > 0:
            logger.info(f"    Loai bo {filtered_published} bai da dang (PUBLISHED)")

        # Tinh diem HOT cho tung bai
        for a in raw_articles:
            a["_hot_score"] = _score_article(a)

        # Sap xep theo diem giam dan va lay top N bai HOT nhat
        raw_articles.sort(key=lambda x: x.get("_hot_score", 0), reverse=True)
        top_articles = raw_articles[:top_per_source]

        logger.info(f"  {source['name']}: {len(raw_articles)} bai -> chon top {len(top_articles)} bai HOT nhat")
        hot_articles_all.extend(top_articles)

    if not hot_articles_all:
        logger.info("  Khong co bai moi nao du dieu kien.")
        _log_run(stats)
        return

    # --- Buoc 4: Chuan hoa ---
    normalized, skipped_norm = normalize_batch(hot_articles_all)
    stats.filtered += skipped_norm

    # Loc keyword
    keyword_passed = [a for a in normalized if a.get("_passes_keyword_filter", True)]
    keyword_dropped = len(normalized) - len(keyword_passed)
    stats.filtered += keyword_dropped
    logger.info(f"  Sau keyword filter: {len(keyword_passed)} bai (bo {keyword_dropped})")

    # --- Buoc 5: Chong trung lap ---
    unique_articles, dup_count = filter_duplicates(keyword_passed)
    stats.url_duplicate = dup_count

    if not unique_articles:
        logger.info("  Khong co bai moi nao sau khi chong trung.")
        _log_run(stats)
        return

    # Gioi han tong so bai moi moi lan chay
    # Sap xep theo diem HOT mot lan nua truoc khi luu
    unique_articles.sort(key=lambda x: x.get("_hot_score", 0), reverse=True)
    unique_articles = unique_articles[:max_per_run]
    logger.info(f"  Giu top {len(unique_articles)} bai HOT nhat trong so {dup_count + len(unique_articles)} bai")

    # --- Buoc 6: Luu vao hang doi (status = QUEUED) ---
    saved = 0
    for article in unique_articles:
        article["status"] = "QUEUED"
        article_id = save_article(article)
        if article_id:
            saved += 1
            score = article.get("_hot_score", 0)
            logger.info(f"    + [{score:.1f}] {article['original_title'][:55]}...")

    max_queue_size = config.get("queue", {}).get("max_queue_size", 50)
    trimmed = enforce_queue_limit(max_queue_size=max_queue_size)
    if trimmed:
        logger.info(f"  Đã chuyển {trimmed} bài dư khỏi giới hạn queue sang EXPIRED.")

    stats.draft_generated = saved

    # --- Thong ke hang doi hien tai ---
    queue_stats = get_queue_stats()
    logger.info(f"  Hang doi hien tai: {queue_stats}")

    _log_run(stats)

    logger.info("=" * 55)
    logger.info(f"[PHASE 1] HOAN THANH: +{saved} bai HOT moi | Tong QUEUED: {queue_stats.get('QUEUED', 0)}")
    logger.info("=" * 55)


def _log_run(stats: RunStats):
    try:
        save_run_log({
            **stats.to_dict(),
            "notes": "collection_phase"
        })
    except Exception:
        pass


if __name__ == "__main__":
    run_collection()
