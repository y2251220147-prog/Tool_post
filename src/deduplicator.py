"""
src/deduplicator.py
Module 3 — Duplicate Detection
Hai cấp độ:
  Level 1: URL exact match (DB check)
  Level 2: Title similarity (difflib fuzzy matching)
"""

import difflib
from .storage import url_exists, hash_exists, get_recent_article_titles
from .logger import get_logger

logger = get_logger("deduplicator")


def is_url_duplicate(source_url: str) -> bool:
    """
    Level 1: Kiểm tra URL đã tồn tại trong DB chưa.
    Nhanh nhất, chạy trước tiên.
    """
    return url_exists(source_url)


def is_hash_duplicate(content_hash: str) -> bool:
    """
    Kiểm tra content hash (title+url) đã tồn tại chưa.
    """
    return hash_exists(content_hash)


def is_title_similar(title: str, existing_titles: list[str], threshold: float = 0.75) -> tuple[bool, str]:
    """
    Level 2: So sánh tiêu đề với các bài gần đây.
    Dùng SequenceMatcher để tính độ tương đồng.
    
    Returns:
        (is_duplicate, matched_title)
    """
    title_lower = title.lower().strip()

    for existing in existing_titles:
        existing_lower = existing.lower().strip()
        ratio = difflib.SequenceMatcher(
            None, title_lower, existing_lower
        ).ratio()

        if ratio >= threshold:
            logger.debug(f"🔁 Title similar ({ratio:.2f}): '{title[:50]}' ~ '{existing[:50]}'")
            return True, existing

    return False, ""


def check_duplicate(article: dict, lookback_days: int = 7, similarity_threshold: float = 0.75) -> dict:
    """
    Kiểm tra duplicate toàn diện cho một bài viết.
    
    Returns:
        {
            "is_duplicate": bool,
            "level": "url" | "hash" | "title" | None,
            "reason": str,
            "matched_title": str
        }
    """
    source_url = article.get("source_url", "")
    content_hash = article.get("content_hash", "")
    title = article.get("original_title", "")

    # Level 1 — URL exact
    if is_url_duplicate(source_url):
        return {
            "is_duplicate": True,
            "level": "url",
            "reason": f"URL đã tồn tại: {source_url}",
            "matched_title": "",
        }

    # Level 1b — Hash
    if content_hash and is_hash_duplicate(content_hash):
        return {
            "is_duplicate": True,
            "level": "hash",
            "reason": "Content hash trùng",
            "matched_title": "",
        }

    # Level 2 — Title similarity
    recent_titles = get_recent_article_titles(days=lookback_days)
    is_dup, matched = is_title_similar(title, recent_titles, threshold=similarity_threshold)
    if is_dup:
        return {
            "is_duplicate": True,
            "level": "title",
            "reason": f"Tiêu đề quá giống bài đã có: '{matched[:80]}'",
            "matched_title": matched,
        }

    return {
        "is_duplicate": False,
        "level": None,
        "reason": "",
        "matched_title": "",
    }


def filter_duplicates(articles: list[dict], lookback_days: int = 3) -> tuple[list[dict], int]:
    """
    Loc duplicate cho mot batch bai viet.
    Toi uu: chi Level-2 title check voi seen_in_batch de tranh O(n^2) khi batch lon.

    Returns:
        (bai khong trung, so bai bi loai)
    """
    unique = []
    duplicate_count = 0
    # Cache titles da thay trong batch nay (tranh batch duplicate)
    seen_in_batch: list[str] = []

    # Load DB titles mot lan cho ca batch (khong load lai tung bai)
    from .storage import get_recent_article_titles
    db_recent_titles = get_recent_article_titles(days=lookback_days)

    for article in articles:
        title = article.get("original_title", "")
        source_url = article.get("source_url", "")
        content_hash = article.get("content_hash", "")

        # Level 1a — URL exact (nhanh nhat, check DB)
        if is_url_duplicate(source_url):
            duplicate_count += 1
            continue

        # Level 1b — Hash exact
        if content_hash and is_hash_duplicate(content_hash):
            duplicate_count += 1
            continue

        # Level 2a — Title similar voi DB titles (da load san)
        is_dup_db, _ = is_title_similar(title, db_recent_titles, threshold=0.80)
        if is_dup_db:
            duplicate_count += 1
            continue

        # Level 2b — Title similar trong batch hien tai
        is_dup_batch, _ = is_title_similar(title, seen_in_batch, threshold=0.82)
        if is_dup_batch:
            duplicate_count += 1
            continue

        unique.append(article)
        seen_in_batch.append(title)

    logger.info(f"Dedup: {len(unique)} unique, {duplicate_count} duplicate")
    return unique, duplicate_count
