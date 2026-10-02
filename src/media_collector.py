"""Thu thập metadata video an toàn từ API chính thức, không tải file video."""

import hashlib
import html
import json
import os
import re
import math
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from .logger import get_logger
from .facebook_auth import get_authorized_pages
from .storage import find_article_by_identity, save_article, update_article_description, upsert_source
from .ucircle_account import get_circle_config


logger = get_logger("media_collector")
ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "media_sources.json"
load_dotenv(ROOT_DIR / ".env", override=True)
_PLATFORM_QUERY_OFFSETS: dict[str, int] = {}


def _config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _published_at(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(platform: str, external_id: str) -> str:
    return hashlib.sha256(f"{platform}:{external_id}".encode("utf-8")).hexdigest()


def _search_query(circle: dict) -> str:
    configured = str(circle.get("media_search_query") or "").strip()
    if configured:
        return configured[:180]
    return str(circle.get("display_name") or circle.get("circle_name") or "tin tức Việt Nam")[:100]


def _plain_search_query(circle: dict) -> str:
    """Chuyển cú pháp Google News có OR/ngoặc kép thành truy vấn từ khóa phổ thông."""
    raw = _search_query(circle)
    cleaned = re.sub(r"\bOR\b", " ", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r'["“”()]+', " ", cleaned)
    words = []
    seen = set()
    for word in cleaned.split():
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            words.append(word)
    return " ".join(words)[:180]


def _search_queries(circle: dict) -> list[str]:
    """Tách một truy vấn OR thành nhiều nhánh để nguồn trả kết quả đa dạng hơn."""
    configured = circle.get("media_search_variants")
    if isinstance(configured, list) and any(str(item).strip() for item in configured):
        variants = [" ".join(str(item).split())[:180] for item in configured if str(item).strip()]
    else:
        raw = _search_query(circle)
        variants = [" ".join(item.split()) for item in re.findall(r'["“]([^"”]+)["”]', raw)]
        if not variants:
            topics = circle.get("topics") if isinstance(circle.get("topics"), list) else []
            circle_name = str(circle.get("display_name") or circle.get("circle_name") or "").strip()
            # Truy vấn dài duy nhất thường luôn trả lại cùng một trang RSS. Tách
            # theo chủ đề để Circle tổng hợp lấy được các kết quả mới đa dạng hơn.
            variants = [
                f"{str(topic).strip()} Việt Nam mới nhất"
                for topic in topics[:8]
                if str(topic).strip()
            ]
            variants.append(raw or circle_name)
    result = []
    seen = set()
    for query in variants:
        cleaned = " ".join(str(query).split()).strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned[:180])
    return result[:12] or [_search_query(circle)]


def _next_platform_query(circle: dict, platform: str) -> str:
    """Luân phiên một truy vấn ngắn mỗi lượt để tiết kiệm quota API YouTube."""
    queries = _search_queries(circle)
    circle_key = str(circle.get("circle_id") or circle.get("circle_name") or "default")
    key = f"{platform}:{circle_key}"
    offset = _PLATFORM_QUERY_OFFSETS.get(key, 0)
    _PLATFORM_QUERY_OFFSETS[key] = offset + 1
    return queries[offset % len(queries)]


def _next_news_queries(circle: dict, batch_size: int = 3) -> list[str]:
    """Luân phiên nhóm truy vấn báo để lượt sau không lặp lại cùng kết quả cũ."""
    queries = _search_queries(circle)
    if len(queries) <= batch_size:
        return queries
    circle_key = str(circle.get("circle_id") or circle.get("circle_name") or "default")
    key = f"news:{circle_key}"
    offset = _PLATFORM_QUERY_OFFSETS.get(key, 0) % len(queries)
    selected = [queries[(offset + index) % len(queries)] for index in range(batch_size)]
    _PLATFORM_QUERY_OFFSETS[key] = (offset + batch_size) % len(queries)
    return selected


def _save_with_candidates(items: list[dict]) -> dict:
    """Lưu nguồn mới và trả cả bài QUEUED đã tồn tại để dùng ngay trong lượt quét."""
    saved = 0
    reused = 0
    candidate_ids = []
    for item in items:
        try:
            article_id = save_article({**item, "status": "QUEUED"})
            if article_id:
                saved += 1
                candidate_ids.append(int(article_id))
                continue
            existing = find_article_by_identity(
                item.get("source_url", ""), item.get("content_hash", "")
            )
            if existing and existing.get("status") == "QUEUED":
                update_article_description(int(existing["id"]), item.get("original_description") or "")
                reused += 1
                candidate_ids.append(int(existing["id"]))
        except Exception as exc:
            logger.warning(f"Không lưu được nguồn media {item.get('source_url', '')}: {exc}")
    return {
        "saved": saved,
        "reused": reused,
        "candidate_ids": list(dict.fromkeys(candidate_ids)),
    }


def _save(items: list[dict]) -> int:
    """Giữ tương thích với các nơi chỉ cần số bài mới."""
    return int(_save_with_candidates(items)["saved"])


def collect_news_search(circle: dict, config: dict) -> dict:
    """Tìm bài theo chủ đề Circle qua RSS Google News; chỉ lưu metadata và liên kết."""
    if not config.get("enabled", True):
        return {"platform": "news_search", "saved": 0, "status": "disabled"}

    import feedparser

    upsert_source({
        "id": "media_google_news", "name": "Google News Search",
        "domain": "news.google.com", "rss_urls": [], "category": "circle_search",
        "source_type": "news_search_rss", "trust_level": 80,
        "requires_attribution": True, "image_policy": "DO_NOT_COPY",
        "language": "vi", "enabled": True,
    })
    max_results = max(1, min(int(config.get("max_results_per_circle", 20)), 50))
    requested_age = circle.get("media_max_age_days") or config.get("max_age_days", 30)
    max_age_days = max(1, min(int(requested_age), 90))
    queries = _next_news_queries(circle)
    per_query = max(5, min(15, math.ceil(max_results / max(1, len(queries)))))
    articles = []
    seen_external_ids = set()
    for search_query in queries:
        query = f"{search_query} when:{max_age_days}d"
        response = requests.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "vi", "gl": "VN", "ceid": "VN:vi"},
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0 AutoUpFeed/2.0"},
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        for entry in feed.entries[:per_query]:
            url = str(getattr(entry, "link", "") or "").strip()
            raw_title = html.unescape(str(getattr(entry, "title", "") or "")).strip()
            if not url or not raw_title:
                continue
            external_id = str(getattr(entry, "id", "") or url)
            if external_id in seen_external_ids:
                continue
            seen_external_ids.add(external_id)
            source_info = getattr(entry, "source", {}) or {}
            source_name = str(source_info.get("title") or "Google News").strip()
            title = raw_title
            suffix = f" - {source_name}"
            if source_name != "Google News" and title.endswith(suffix):
                title = title[:-len(suffix)].strip()
            description = html.unescape(str(getattr(entry, "summary", "") or ""))
            description = " ".join(re.sub(r"<[^>]+>", " ", description).split())
            published_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                published_at = parsedate_to_datetime(str(getattr(entry, "published", ""))).strftime("%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError, OverflowError):
                pass
            articles.append({
                "source_id": "media_google_news",
                "source_name": source_name,
                "source_url": url,
                "original_title": title,
                "original_description": description,
                "author": source_name,
                "published_at": published_at,
                "category": "circle_search",
                "content_hash": _hash("google_news", external_id),
                "source_image_url": "",
                "platform": "news",
                "media_type": "article",
                "external_id": external_id,
                "embed_url": "",
                "thumbnail_url": "",
                "license_type": "unknown",
                "rights_status": "LINK_ONLY",
            })
            if len(articles) >= max_results:
                break
        if len(articles) >= max_results:
            break
    save_result = _save_with_candidates(articles)
    return {
        "platform": "news_search", "saved": save_result["saved"],
        "reused": save_result["reused"], "candidate_ids": save_result["candidate_ids"],
        "found": len(articles),
        "queries": len(queries), "status": "ok",
    }


def collect_youtube(circle: dict, config: dict) -> dict:
    """Tìm video Creative Commons bằng YouTube Data API; chỉ lưu metadata và URL."""
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not config.get("enabled", False):
        return {"platform": "youtube", "saved": 0, "status": "disabled"}
    if not api_key:
        return {"platform": "youtube", "saved": 0, "status": "missing_key"}

    upsert_source({
        "id": "media_youtube_cc", "name": "YouTube Creative Commons",
        "domain": "youtube.com", "rss_urls": [], "category": "video",
        "source_type": "youtube_api", "trust_level": 82,
        "requires_attribution": True, "image_policy": "DO_NOT_COPY",
        "language": "vi", "enabled": True,
    })
    params = {
        "part": "snippet", "type": "video", "q": _next_platform_query(circle, "youtube"),
        "maxResults": max(1, min(int(config.get("max_results_per_circle", 5)), 20)),
        "order": "relevance", "safeSearch": "strict", "key": api_key,
        "videoLicense": "creativeCommon",
        "regionCode": config.get("region_code", "VN"),
        "relevanceLanguage": config.get("relevance_language", "vi"),
    }
    max_age_days = max(1, min(int(circle.get("media_max_age_days") or 90), 90))
    params["publishedAfter"] = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    if config.get("embeddable_only", True):
        params["videoEmbeddable"] = "true"
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params=params, timeout=25,
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    link_only_fallback = False
    if not items and config.get("allow_link_only_fallback", True):
        # Không có video Creative Commons: vẫn có thể chia sẻ liên kết/nhúng
        # video công khai, nhưng tuyệt đối không tải hay đăng lại nội dung.
        fallback_params = dict(params)
        fallback_params.pop("videoLicense", None)
        fallback_response = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=fallback_params,
            timeout=25,
        )
        fallback_response.raise_for_status()
        items = fallback_response.json().get("items", [])
        link_only_fallback = bool(items)
    articles = []
    details_by_id = {}
    video_ids = [str(item.get("id", {}).get("videoId") or "") for item in items]
    video_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
    if video_ids:
        try:
            # Search chỉ trả mô tả ngắn. Một yêu cầu metadata cho cả nhóm giúp
            # Analyzer nhận đủ thông tin; không tải video hay tự suy đoán quyền.
            details_response = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet,status", "id": ",".join(video_ids), "key": api_key},
                timeout=25,
            )
            details_response.raise_for_status()
            details_by_id = {
                str(item.get("id")): item
                for item in details_response.json().get("items", [])
                if isinstance(item.get("id"), str)
            }
        except Exception:
            # Không đưa URL ngoại lệ vào log vì requests có thể chứa API key.
            logger.warning("Không đọc được metadata đầy đủ YouTube; giữ kết quả tìm kiếm, không tải video.")
    for item in items:
        video_id = str(item.get("id", {}).get("videoId") or "").strip()
        details = details_by_id.get(video_id, {})
        snippet = details.get("snippet") or item.get("snippet", {})
        if not video_id:
            continue
        verified_license = details.get("status", {}).get("license")
        reusable = verified_license == "creativeCommon" or (not verified_license and not link_only_fallback)
        url = f"https://www.youtube.com/watch?v={video_id}"
        articles.append({
            "source_id": "media_youtube_cc",
            "source_name": f"YouTube · {snippet.get('channelTitle', 'Kênh chưa rõ')}",
            "source_url": url,
            "original_title": snippet.get("title", ""),
            "original_description": snippet.get("description", ""),
            "author": snippet.get("channelTitle", ""),
            "published_at": _published_at(snippet.get("publishedAt", "")),
            "category": "video",
            "content_hash": _hash("youtube", video_id),
            "source_image_url": "",
            "platform": "youtube",
            "media_type": "video",
            "external_id": video_id,
            "embed_url": f"https://www.youtube.com/embed/{video_id}",
            "thumbnail_url": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
            "license_type": "creativeCommon" if reusable else "unknown",
            "rights_status": "REUSABLE_WITH_ATTRIBUTION" if reusable else "LINK_ONLY",
        })
    save_result = _save_with_candidates(articles)
    return {
        "platform": "youtube",
        "saved": save_result["saved"],
        "reused": save_result["reused"],
        "candidate_ids": save_result["candidate_ids"],
        "found": len(articles),
        "status": "link_only" if link_only_fallback else "ok",
    }


def collect_facebook(circle: dict, config: dict) -> dict:
    """Đọc Feed/Reels từ các Page được cấu hình; không tìm kiếm hoặc tải video công khai."""
    token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    configured_pages = config.get("pages", []) if isinstance(config.get("pages"), list) else []
    configured_by_id = {str(page.get("page_id")): page for page in configured_pages}
    authorized_pages = get_authorized_pages()
    pages = []
    for page in authorized_pages:
        merged = dict(configured_by_id.get(str(page.get("page_id")), {}))
        merged.update(page)
        pages.append(merged)
    if not pages:
        pages = configured_pages
    if not config.get("enabled", False):
        return {"platform": "facebook", "saved": 0, "status": "disabled"}
    if not pages or not any(page.get("access_token") or token for page in pages):
        return {"platform": "facebook", "saved": 0, "status": "missing_page_config"}

    upsert_source({
        "id": "media_facebook_pages", "name": "Facebook Pages được cấp quyền",
        "domain": "facebook.com", "rss_urls": [], "category": "social",
        "source_type": "facebook_graph_api", "trust_level": 80,
        "requires_attribution": True, "image_policy": "DO_NOT_COPY",
        "language": "vi", "enabled": True,
    })
    version = str(config.get("graph_api_version", "v23.0")).strip()
    limit = max(1, min(int(config.get("max_results_per_page", 5)), 20))
    query = _plain_search_query(circle).casefold()
    words = [word for word in query.split() if len(word) >= 3][:8]
    articles = []
    for page in pages:
        page_id = str(page.get("page_id") or "").strip()
        if not page_id:
            continue
        page_token = str(page.get("access_token") or token).strip()
        if not page_token:
            continue
        response = requests.get(
            f"https://graph.facebook.com/{version}/{page_id}/feed",
            params={
                "access_token": page_token, "limit": limit,
                "fields": "id,message,permalink_url,created_time,from,attachments{media_type,title,description,url}",
            }, timeout=25,
        )
        response.raise_for_status()
        for item in response.json().get("data", []):
            message = str(item.get("message") or "").strip()
            permalink = str(item.get("permalink_url") or "").strip()
            if not permalink or (words and not any(word in message.casefold() for word in words)):
                continue
            external_id = str(item.get("id") or permalink)
            is_reel = "/reel/" in permalink or "/reels/" in permalink
            rights_confirmed = bool(page.get("rights_confirmed", False))
            articles.append({
                "source_id": "media_facebook_pages",
                "source_name": f"Facebook · {page.get('name') or item.get('from', {}).get('name', page_id)}",
                "source_url": permalink,
                "original_title": (message[:160] or f"Nội dung Facebook {external_id}"),
                "original_description": message,
                "author": item.get("from", {}).get("name", ""),
                "published_at": _published_at(item.get("created_time", "")),
                "category": "social_video" if is_reel else "social",
                "content_hash": _hash("facebook", external_id),
                "source_image_url": "",
                "platform": "facebook_reel" if is_reel else "facebook",
                "media_type": "reel" if is_reel else "post",
                "external_id": external_id,
                "embed_url": "",
                "thumbnail_url": "",
                "license_type": "owned_or_authorized" if rights_confirmed else "unknown",
                "rights_status": "OWNED" if rights_confirmed else "LINK_ONLY",
            })
    save_result = _save_with_candidates(articles)
    return {
        "platform": "facebook", "saved": save_result["saved"],
        "reused": save_result["reused"], "candidate_ids": save_result["candidate_ids"],
        "found": len(articles),
        "status": "ok" if articles else "no_matching_authorized_page_posts",
    }


def collect_media_for_circle(circle_id: str) -> dict:
    """Thu thập các nền tảng đã bật cho Circle; lỗi một nguồn không làm hỏng nguồn khác."""
    circle = get_circle_config(circle_id)
    platforms = set(circle.get("media_platforms") or ["news"])
    config = _config()
    results = []
    collectors = []
    if "news" in platforms:
        collectors.append((collect_news_search, config.get("news_search", {})))
    if "youtube" in platforms:
        collectors.append((collect_youtube, config.get("youtube", {})))
    if platforms.intersection({"facebook", "facebook_reel"}):
        collectors.append((collect_facebook, config.get("facebook", {})))
    for collector, platform_config in collectors:
        try:
            results.append(collector(circle, platform_config))
        except Exception as exc:
            logger.error(f"Lỗi thu thập media cho {circle_id}: {exc}")
            results.append({"platform": collector.__name__, "saved": 0, "status": "error", "error": str(exc)[:240]})
    candidate_ids = list(dict.fromkeys(
        article_id
        for item in results
        for article_id in item.get("candidate_ids", [])
    ))
    return {
        "saved": sum(item.get("saved", 0) for item in results),
        "reused": sum(item.get("reused", 0) for item in results),
        "candidate_ids": candidate_ids,
        "sources": results,
    }
