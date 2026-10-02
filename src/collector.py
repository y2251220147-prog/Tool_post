"""
src/collector.py
Module 1 — RSS Source Collector
Thu thập bài viết mới từ các nguồn RSS đã được cấu hình trong sources.json
"""

import feedparser
import hashlib
import httpx
from datetime import datetime, timezone
from urllib.parse import urljoin

from .storage import get_enabled_sources
from .logger import get_logger

logger = get_logger("collector")

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AutoUpFeed/2.0",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
MAX_ENTRIES_PER_FEED = 100


def _parse_published_at(entry) -> str:
    """Parse thời gian đăng bài từ RSS entry."""
    # feedparser tự parse thành time.struct_time
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    if hasattr(entry, "updated_parsed") and entry.updated_parsed:
        try:
            dt = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _make_content_hash(title: str, url: str) -> str:
    """Tạo hash từ title + url để detect duplicate nhanh."""
    text = f"{title.strip().lower()}|{url.strip().lower()}"
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _get_description(entry) -> str:
    """Lấy mô tả từ RSS entry, ưu tiên summary."""
    desc = ""
    if hasattr(entry, "summary") and entry.summary:
        desc = entry.summary
    elif hasattr(entry, "description") and entry.description:
        desc = entry.description
    # Loại bỏ HTML tags đơn giản
    import re
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    return desc[:2000]  # Giới hạn 2000 ký tự


def _get_author(entry) -> str:
    """Lấy tên tác giả nếu có."""
    if hasattr(entry, "author") and entry.author:
        return entry.author[:100]
    if hasattr(entry, "authors") and entry.authors:
        return entry.authors[0].get("name", "")[:100]
    return ""


def _get_source_image(entry, article_url: str) -> str:
    """Lấy URL ảnh đại diện từ các trường RSS phổ biến."""
    candidates = []
    for field_name in ("media_content", "media_thumbnail"):
        values = getattr(entry, field_name, None) or []
        for item in values:
            if isinstance(item, dict):
                candidates.append(item.get("url", ""))

    for enclosure in getattr(entry, "enclosures", None) or []:
        if not isinstance(enclosure, dict):
            continue
        mime_type = str(enclosure.get("type", "")).lower()
        if mime_type.startswith("image/"):
            candidates.append(enclosure.get("href") or enclosure.get("url") or "")

    for candidate in candidates:
        candidate = str(candidate or "").strip()
        if not candidate:
            continue
        absolute_url = urljoin(article_url, candidate)
        if absolute_url.startswith(("http://", "https://")):
            return absolute_url
    return ""


def collect_from_source(source: dict) -> list[dict]:
    """
    Thu thập bài viết từ một source.
    
    Returns:
        List các dict bài viết thô (chưa normalize, chưa lưu DB)
    """
    articles = []
    source_id = source["id"]
    source_name = source["name"]
    rss_urls = source.get("rss_urls", [])

    for rss_url in rss_urls:
        try:
            logger.info(f"📡 Đang đọc RSS: {source_name} — {rss_url}")
            with httpx.Client(timeout=15.0, follow_redirects=True, headers=REQUEST_HEADERS) as client:
                response = client.get(rss_url)
                response.raise_for_status()
            feed = feedparser.parse(response.content)

            if feed.bozo and not feed.entries:
                error_text = str(getattr(feed, "bozo_exception", "RSS không hợp lệ"))[:180]
                logger.warning(f"⚠️ RSS lỗi hoặc không hợp lệ: {rss_url} — {error_text}")
                continue

            count_from_this_feed = 0
            max_entries = max(1, int(source.get("max_entries_per_feed", MAX_ENTRIES_PER_FEED)))
            for entry in feed.entries[:max_entries]:
                # Lấy URL bài gốc
                link = getattr(entry, "link", "").strip()
                if not link:
                    continue

                # Lấy tiêu đề
                title = getattr(entry, "title", "").strip()
                if not title or len(title) < 5:
                    continue

                article = {
                    "source_id": source_id,
                    "source_name": source_name,
                    "source_url": link,
                    "original_title": title,
                    "original_description": _get_description(entry),
                    "author": _get_author(entry),
                    "published_at": _parse_published_at(entry),
                    "category": source.get("category", "news"),
                    "content_hash": _make_content_hash(title, link),
                    "source_image_url": _get_source_image(entry, link),
                    "status": "NEW",
                    # Metadata thêm (không lưu DB, dùng trong pipeline)
                    "_trust_level": source.get("trust_level", 80),
                    "_requires_attribution": source.get("requires_attribution", True),
                    "_image_policy": source.get("image_policy", "DO_NOT_COPY"),
                }
                articles.append(article)
                count_from_this_feed += 1

            logger.info(f"  ✅ {source_name}: {count_from_this_feed} bài từ {rss_url}")

        except httpx.HTTPStatusError as e:
            logger.warning(f"⚠️ RSS trả về HTTP {e.response.status_code}: {rss_url}")
            continue
        except Exception as e:
            logger.error(f"❌ Lỗi đọc RSS {rss_url}: {str(e)[:200]}")
            continue

    return articles


def collect_all() -> list[dict]:
    """
    Thu thập bài viết từ tất cả sources đang bật.
    
    Returns:
        List tất cả bài viết thô từ mọi sources
    """
    sources = get_enabled_sources()
    if not sources:
        logger.warning("⚠️ Không có source nào được bật trong config!")
        return []

    logger.info(f"🔍 Bắt đầu thu thập từ {len(sources)} sources...")
    all_articles = []

    for source in sources:
        articles = collect_from_source(source)
        all_articles.extend(articles)

    logger.info(f"📦 Tổng cộng thu thập được: {len(all_articles)} bài")
    return all_articles
