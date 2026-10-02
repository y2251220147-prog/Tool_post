"""Chọn bài trong hàng đợi phù hợp prompt riêng của từng Circle."""

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from .gemini_client import generate_json
from .analyzer import _publish_threshold
from .logger import get_logger
from .storage import (
    get_circle_article_queue,
    get_connection,
    recover_legacy_circle_rejections,
    save_circle_article_matches,
)
from .ucircle_account import get_circle_config


logger = get_logger("circle_selector")
AUTOMATION_CONFIG = Path(__file__).resolve().parent.parent / "config" / "automation.json"


def _normalize_search_text(value: str) -> str:
    value = unicodedata.normalize("NFD", str(value or "").replace("Đ", "D").replace("đ", "d"))
    value = "".join(char for char in value if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _is_near_duplicate_title(title: str, published_titles: list[str]) -> bool:
    """Nhận diện cùng nội dung dù nguồn đổi nhẹ tiêu đề hoặc AI đã viết lại tiêu đề."""
    normalized = _normalize_search_text(title)
    if len(normalized) < 18:
        return False
    tokens = set(normalized.split())
    for old_title in published_titles:
        old_normalized = _normalize_search_text(old_title)
        if len(old_normalized) < 18:
            continue
        if normalized == old_normalized:
            return True
        old_tokens = set(old_normalized.split())
        union = tokens | old_tokens
        token_similarity = len(tokens & old_tokens) / len(union) if union else 0
        sequence_similarity = SequenceMatcher(None, normalized, old_normalized).ratio()
        # Cần ít nhất bốn từ chung để tránh loại nhầm các tiêu đề ngắn cùng chủ đề.
        if len(tokens & old_tokens) >= 4 and (
            token_similarity >= 0.72 or sequence_similarity >= 0.84
        ):
            return True
    return False


def _local_keyword_matches(circle: dict, candidates: list[dict], max_matches: int) -> list[dict]:
    """Bộ lọc dự phòng khi Gemini tạm quá tải; không thay thế AI khi AI sẵn sàng."""
    query = _normalize_search_text(
        circle.get("media_search_query") or circle.get("display_name") or circle.get("circle_name")
    )
    ignored = {
        "tin", "tuc", "moi", "nhat", "hay", "chinh", "thuc", "review",
        "viet", "nam", "noi", "dung", "nguon", "dia", "phuong",
    }
    keywords = {
        word for word in query.split()
        if len(word) >= 3 and word not in ignored
    }
    circle_id = str(circle.get("circle_id") or "")
    is_general_news = (
        circle.get("prompt_profile_id") == "tin_tuc_24h"
        or circle_id == "circle_tintuc24h"
    )
    fallback_filter = circle.get("fallback_filter") if isinstance(circle.get("fallback_filter"), dict) else {}
    all_groups = fallback_filter.get("all_groups", [])
    title_required_groups = fallback_filter.get("title_required_groups", [])
    excluded_phrases = fallback_filter.get("exclude", [])
    excluded_title_phrases = fallback_filter.get("title_exclude", [])
    priority_phrases = fallback_filter.get("priority_phrases", [])
    deprioritized_sources = fallback_filter.get("deprioritize_sources", [])

    def contains_phrase(text: str, phrase: str) -> bool:
        normalized_phrase = _normalize_search_text(phrase)
        return bool(normalized_phrase) and f" {normalized_phrase} " in f" {text} "

    ranked = []

    for position, item in enumerate(candidates):
        title = _normalize_search_text(item.get("original_title", ""))
        description = _normalize_search_text(item.get("original_description", ""))
        category = _normalize_search_text(item.get("category", ""))
        combined = " ".join((title, description, category))
        if any(contains_phrase(title, phrase) for phrase in excluded_title_phrases):
            continue
        if any(contains_phrase(combined, phrase) for phrase in excluded_phrases):
            continue
        if all_groups:
            group_combined_hits = [
                isinstance(group, list)
                and any(contains_phrase(combined, phrase) for phrase in group)
                for group in all_groups
            ]
            group_title_hits_flags = [
                isinstance(group, list)
                and any(contains_phrase(title, phrase) for phrase in group)
                for group in all_groups
            ]
            # Mô tả RSS đôi khi ghép các cụm chung như "hướng dẫn", "ứng dụng"
            # vào một tin hoàn toàn khác chủ đề. Mỗi nhóm vẫn phải khớp toàn văn,
            # đồng thời tiêu đề phải thể hiện ít nhất một nhóm để tránh dương tính giả.
            if not all(group_combined_hits) or not any(group_title_hits_flags):
                continue
        if title_required_groups and not all(
            isinstance(group, list) and any(contains_phrase(title, phrase) for phrase in group)
            for group in title_required_groups
        ):
            continue

        title_hits = sum(1 for word in keywords if contains_phrase(title, word))
        description_hits = sum(1 for word in keywords if contains_phrase(description, word))
        category_hits = sum(1 for word in keywords if contains_phrase(category, word))
        total_hits = title_hits + description_hits + category_hits
        priority_hits = sum(1 for phrase in priority_phrases if contains_phrase(title, phrase))

        if is_general_news and item.get("platform", "news") == "news":
            score = min(95, max(65, 82 - position) + min(priority_hits, 3) * 8)
        elif all_groups:
            group_title_hits = sum(
                1 for group in all_groups
                if any(contains_phrase(title, phrase) for phrase in group)
            )
            score = min(95, 65 + group_title_hits * 6 + min(title_hits, 3) * 2
                        + min(description_hits, 2) + min(priority_hits, 3) * 6)
        elif title_hits >= 1 or total_hits >= 2:
            score = min(95, 60 + title_hits * 12 + description_hits * 4 + category_hits * 6)
        else:
            continue
        if len(str(item.get("original_description") or "").split()) < 90 and any(
            contains_phrase(_normalize_search_text(item.get("source_name", "")), source)
            for source in deprioritized_sources
        ):
            # Trang tổng hợp video ít dữ kiện không nên che mất bài thuật lại
            # tình huống có thể đọc được; vẫn giữ làm ứng viên sau nếu cần.
            score = max(60, score - 20)
        ranked.append((score, position, item))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [
        {
            "article_id": int(item["id"]),
            "relevance_score": score,
            "reason": "Bộ lọc dự phòng khớp từ khóa trong tiêu đề, mô tả hoặc chuyên mục khi Gemini tạm quá tải.",
        }
        for score, _position, item in ranked[:max_matches]
    ]


def _queued_candidates(
    limit: int = 25,
    platforms: list[str] | None = None,
    circle_id: str = "",
    preferred_article_ids: list[int] | None = None,
    max_age_days: int = 90,
) -> list[dict]:
    """Lấy hồ ứng viên rộng để tin của Circle khác không che mất tin phù hợp."""
    # Các Circle dùng chung bảng articles. Hồ quá nhỏ khiến tin bóng đá/thời sự
    # mới hơn che mất tin ngách như ẩm thực địa phương dù dữ liệu vẫn tồn tại.
    pool_limit = max(300, min(max(1, int(limit)) * 15, 500))
    conn = get_connection()
    try:
        preferred_ids = list(dict.fromkeys(int(value) for value in preferred_article_ids or []))[:500]
        preferred_placeholders = ",".join("?" for _ in preferred_ids)
        preferred_sort = (
            f"CASE WHEN a.id IN ({preferred_placeholders}) THEN 0 ELSE 1 END,"
            if preferred_ids else ""
        )
        rows = conn.execute(
            f"""
            SELECT a.id, a.original_title, a.original_description, a.source_url,
                   a.category, a.published_at, a.source_name, s.trust_level,
                   COALESCE(a.platform, 'news') AS platform,
                   COALESCE(a.media_type, 'article') AS media_type,
                   COALESCE(a.license_type, 'unknown') AS license_type,
                   COALESCE(a.rights_status, 'LINK_ONLY') AS rights_status
            FROM articles a
            JOIN sources s ON a.source_id = s.id
            WHERE a.status = 'QUEUED'
              AND (
                  a.published_at IS NULL OR trim(a.published_at) = ''
                  OR datetime(a.published_at) >= datetime('now', ?)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM generated_posts p
                  WHERE p.status IN ('DRAFT', 'REVIEW', 'READY', 'PUBLISHED')
                    AND lower(trim(COALESCE(p.ai_title, ''))) = lower(trim(a.original_title))
              )
              AND NOT EXISTS (
                  SELECT 1 FROM circle_article_queue q
                  WHERE q.article_id = a.id
                    AND q.status IN ('MATCHED', 'PROCESSING', 'GENERATED')
              )
            ORDER BY
                {preferred_sort}
                CASE WHEN a.category = 'circle_search' THEN 0 ELSE 1 END,
                a.collected_at DESC,
                a.published_at DESC,
                s.trust_level DESC,
                a.id DESC
            LIMIT ?
            """,
            (f"-{max(1, min(int(max_age_days or 90), 90))} days", *preferred_ids, pool_limit),
        ).fetchall()
        published_rows = conn.execute("""
            SELECT p.source_url, p.ai_title, a.original_title
            FROM generated_posts p
            LEFT JOIN articles a ON a.id = p.article_id
            WHERE p.status IN ('DRAFT', 'REVIEW', 'READY', 'PUBLISHED')
        """).fetchall()
        allowed = set(platforms or ["news"])
        published_urls = {
            str(row["source_url"] or "").strip() for row in published_rows if row["source_url"]
        }
        published_titles = [
            str(value or "").strip()
            for row in published_rows
            for value in (row["ai_title"], row["original_title"])
            if value
        ]
        result = []
        rejected_for_circle = set()
        if circle_id:
            rejected_for_circle = {
                int(row[0])
                for row in conn.execute(
                    "SELECT article_id FROM circle_article_queue "
                    "WHERE circle_id = ? AND status = 'REJECTED'",
                    (circle_id,),
                ).fetchall()
            }
        for row in rows:
            item = dict(row)
            if int(item["id"]) in rejected_for_circle:
                continue
            if item.get("platform", "news") not in allowed:
                continue
            if str(item.get("source_url") or "").strip() in published_urls:
                continue
            if _is_near_duplicate_title(item.get("original_title", ""), published_titles):
                continue
            result.append(item)
        preferred_order = {
            int(article_id): position
            for position, article_id in enumerate(preferred_article_ids or [])
        }
        if preferred_order:
            result.sort(key=lambda item: (
                0 if int(item["id"]) in preferred_order else 1,
                preferred_order.get(int(item["id"]), len(preferred_order)),
            ))
        return result[:pool_limit]
    finally:
        conn.close()


def _prioritize_for_ai(circle: dict, candidates: list[dict], limit: int) -> list[dict]:
    """Đưa tin khớp bộ lọc Circle lên trước rồi mới bổ sung tin còn lại."""
    local_ranked = _local_keyword_matches(circle, candidates, max_matches=len(candidates))
    by_id = {int(item["id"]): item for item in candidates}
    prioritized = []
    seen = set()
    for match in local_ranked:
        article_id = int(match["article_id"])
        if article_id in by_id and article_id not in seen:
            prioritized.append(by_id[article_id])
            seen.add(article_id)
    fallback_filter = circle.get("fallback_filter") if isinstance(circle.get("fallback_filter"), dict) else {}
    # Circle chuyên đề có all_groups là điều kiện bắt buộc, không chỉ là điểm ưu tiên.
    # Không đưa nguồn lạc chủ đề vào cuối danh sách để AI "chọn cho đủ".
    if fallback_filter.get("all_groups"):
        return prioritized[:max(1, min(int(limit), 50))]
    for item in candidates:
        article_id = int(item["id"])
        if article_id not in seen:
            prioritized.append(item)
            seen.add(article_id)
    return prioritized[:max(1, min(int(limit), 50))]


def _save_first_local_match(circle_id: str, circle: dict, candidates: list[dict]) -> tuple[int, dict | None]:
    """Giữ đúng một ứng viên cục bộ; bỏ qua ứng viên đang thuộc hàng Circle khác."""
    local_matches = _local_keyword_matches(circle, candidates, min(100, len(candidates)))
    for match in local_matches:
        saved = save_circle_article_matches(circle_id, [match])
        if saved:
            return saved, match
    return 0, None


def _skip_stale_published_matches(circle_id: str) -> int:
    """Dọn hàng MATCHED cũ nếu nguồn hoặc nội dung gần giống bài đã đăng."""
    conn = get_connection()
    try:
        published_rows = conn.execute("""
            SELECT p.source_url, p.ai_title, a.original_title
            FROM generated_posts p
            LEFT JOIN articles a ON a.id = p.article_id
            WHERE p.status IN ('DRAFT', 'REVIEW', 'READY', 'PUBLISHED')
        """).fetchall()
        published_urls = {
            str(row["source_url"] or "").strip() for row in published_rows if row["source_url"]
        }
        published_titles = [
            str(value or "").strip()
            for row in published_rows
            for value in (row["ai_title"], row["original_title"])
            if value
        ]
        rows = conn.execute("""
            SELECT q.article_id, a.source_url, a.original_title
            FROM circle_article_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE q.circle_id = ? AND q.status = 'MATCHED' AND a.status = 'QUEUED'
        """, (circle_id,)).fetchall()
        duplicate_ids = [
            int(row["article_id"])
            for row in rows
            if str(row["source_url"] or "").strip() in published_urls
            or _is_near_duplicate_title(row["original_title"], published_titles)
        ]
        if not duplicate_ids:
            return 0
        placeholders = ",".join("?" for _ in duplicate_ids)
        conn.execute(
            f"UPDATE circle_article_queue SET status='SKIPPED', updated_at=datetime('now','localtime') "
            f"WHERE circle_id=? AND article_id IN ({placeholders})",
            (circle_id, *duplicate_ids),
        )
        conn.execute(
            f"UPDATE articles SET status='DUPLICATE', updated_at=datetime('now','localtime') "
            f"WHERE id IN ({placeholders}) AND status='QUEUED'",
            duplicate_ids,
        )
        conn.commit()
        return len(duplicate_ids)
    finally:
        conn.close()


def select_article_for_circle(circle_id: str, limit: int = 12) -> dict:
    circle = get_circle_config(circle_id)
    if not circle:
        return {"success": False, "error": f"Không tìm thấy cấu hình Circle '{circle_id}'."}
    if not circle.get("ownership_verified"):
        return {"success": False, "error": "Circle chưa được xác minh quyền sở hữu."}
    if not circle.get("scan_enabled", False):
        return {"success": False, "error": "Circle chưa bật tùy chọn Quét bài."}

    candidates = _queued_candidates(limit, circle.get("media_platforms") or ["news"])
    if not candidates:
        return {"success": False, "error": "Hàng đợi không còn tin nào để quét."}

    ai_candidates = _prioritize_for_ai(circle, candidates, limit)
    compact_candidates = [
        {
            "id": item["id"],
            "title": item.get("original_title", ""),
            "description": (item.get("original_description") or "")[:350],
            "category": item.get("category", ""),
            "source": item.get("source_name", ""),
            "published_at": item.get("published_at", ""),
            "trust_level": item.get("trust_level", 0),
            "platform": item.get("platform", "news"),
            "media_type": item.get("media_type", "article"),
            "license": item.get("license_type", "unknown"),
            "rights_status": item.get("rights_status", "LINK_ONLY"),
        }
        for item in ai_candidates
    ]
    prompt = f"""
Bạn là bộ chọn tin cho Circle "{circle.get('circle_name', circle_id)}".

Quy tắc chung bắt buộc:
{circle.get('selection_prompt', '')}

Yêu cầu nội dung riêng của Circle:
{circle.get('content_prompt', '')}

Danh sách tin ứng viên:
{json.dumps(compact_candidates, ensure_ascii=False)}

Chọn đúng một tin nổi bật nhất trong số các tin phù hợp. Ưu tiên tin mới, nguồn uy tín,
sự kiện đang được quan tâm và có giá trị chia sẻ. Không chọn tin chỉ liên quan mơ hồ hoặc lệch chủ đề.
Trả về JSON thuần:
{{
  "selected_article_id": 0,
  "relevance_score": 0,
  "reason": "lý do ngắn gọn"
}}
Nếu không có tin phù hợp, selected_article_id phải là 0 và relevance_score dưới 60.
""".strip()

    ai_config = {}
    try:
        with open(AUTOMATION_CONFIG, encoding="utf-8") as file:
            ai_config = json.load(file).get("ai", {})
    except (OSError, ValueError, TypeError):
        pass

    try:
        result = generate_json(
            prompt,
            temperature=0.1,
            purpose=f"chọn tin cho {circle.get('circle_name', circle_id)}",
            preferred_model=ai_config.get("model_text"),
        )
    except Exception as exc:
        logger.error(f"Không thể chọn tin cho Circle {circle_id}: {exc}")
        fallback = _local_keyword_matches(circle, candidates, 1)
        if fallback:
            selected = fallback[0]
            return {
                "success": True,
                "article_id": selected["article_id"],
                "relevance_score": selected["relevance_score"],
                "reason": selected["reason"],
                "circle_id": circle_id,
                "selection_mode": "local_fallback",
                "warning": "Gemini đang bận; đã dùng bộ lọc từ khóa dự phòng trên máy.",
            }
        return {
            "success": False,
            "error": "Gemini đang tạm quá tải và bộ lọc dự phòng chưa tìm thấy tin phù hợp.",
            "selection_mode": "local_fallback",
        }

    try:
        selected_id = int(result.get("selected_article_id") or 0)
        score = int(result.get("relevance_score") or 0)
    except (TypeError, ValueError):
        selected_id, score = 0, 0
    valid_ids = {item["id"] for item in ai_candidates}
    if selected_id not in valid_ids or score < 60:
        return {
            "success": False,
            "error": "Chưa có tin nào đủ phù hợp với prompt của Circle.",
            "reason": result.get("reason", ""),
            "relevance_score": score,
        }

    return {
        "success": True,
        "article_id": selected_id,
        "relevance_score": score,
        "reason": result.get("reason", ""),
        "circle_id": circle_id,
    }


def scan_articles_for_circle(
    circle_id: str,
    limit: int = 12,
    max_matches: int = 1,
    preferred_article_ids: list[int] | None = None,
) -> dict:
    """Chọn đúng một tin nổi bật nhất cho một Circle để tiết kiệm hạn mức AI."""
    circle = get_circle_config(circle_id)
    if not circle:
        return {"success": False, "error": f"Không tìm thấy cấu hình Circle '{circle_id}'."}
    if not circle.get("ownership_verified"):
        return {"success": False, "error": "Circle chưa được xác minh quyền sở hữu."}
    if not circle.get("scan_enabled", False):
        return {"success": False, "error": "Circle chưa bật tùy chọn Quét bài."}
    if not str(circle.get("content_prompt", "")).strip():
        return {"success": False, "error": "Circle chưa có prompt nội dung riêng."}

    restored_legacy = recover_legacy_circle_rejections()
    if restored_legacy:
        logger.info(
            f"Đã trả {restored_legacy} tin bị loại theo kiểu cũ về kho dùng chung."
        )

    skipped_duplicates = _skip_stale_published_matches(circle_id)
    if skipped_duplicates:
        logger.info(
            f"Đã bỏ {skipped_duplicates} tin MATCHED trùng bài đã đăng của Circle {circle_id}."
        )

    candidates = _queued_candidates(
        limit,
        circle.get("media_platforms") or ["news"],
        circle_id=circle_id,
        preferred_article_ids=preferred_article_ids,
        max_age_days=circle.get("media_max_age_days") or 90,
    )
    if not candidates:
        return {"success": False, "error": "Không còn tin chưa phân loại trong hàng đợi chung."}

    ai_candidates = _prioritize_for_ai(circle, candidates, limit)
    fallback_filter = circle.get("fallback_filter") if isinstance(circle.get("fallback_filter"), dict) else {}
    if not ai_candidates:
        logger.info(f"{circle.get('circle_name', circle_id)}: {len(candidates)} nguồn khả dụng, 0 vượt bộ lọc chủ đề.")
        return {
            "success": True,
            "matched": 0,
            "message": f"Có {len(candidates)} nguồn chưa dùng nhưng chưa có nguồn vượt bộ lọc chủ đề/địa điểm của Circle.",
            "candidate_count": len(candidates),
            "selection_mode": "local_no_match",
            "circle_id": circle_id,
        }
    if fallback_filter.get("all_groups") and ai_candidates:
        # Với Circle chuyên đề, ứng viên ở đây đã vượt toàn bộ nhóm từ khóa bắt
        # buộc và điều kiện loại trừ. Chọn trực tiếp ứng viên tốt nhất để tránh
        # tốn thêm một lượt Gemini và tránh AI đổi sang nguồn lạc chủ đề.
        saved, local_match = _save_first_local_match(circle_id, circle, ai_candidates)
        if local_match:
            return {
                "success": True,
                "matched": saved,
                "evaluated_matches": 1,
                "selected_article_id": local_match["article_id"],
                "message": "Đã chọn một tin vượt bộ lọc chủ đề chặt của Circle.",
                "circle_id": circle_id,
                "selection_mode": "local_strict",
            }

    compact_candidates = [
        {
            "id": item["id"],
            "title": item.get("original_title", ""),
            "description": (item.get("original_description") or "")[:500],
            "category": item.get("category", ""),
            "source": item.get("source_name", ""),
            "published_at": item.get("published_at", ""),
            "trust_level": item.get("trust_level", 0),
            "platform": item.get("platform", "news"),
            "media_type": item.get("media_type", "article"),
            "license": item.get("license_type", "unknown"),
            "rights_status": item.get("rights_status", "LINK_ONLY"),
        }
        for item in ai_candidates
    ]
    # Luôn giới hạn một kết quả dù phía gọi truyền giá trị cũ lớn hơn.
    max_matches = 1
    prompt = f"""
Bạn là bộ phân loại tin cho Circle "{circle.get('circle_name', circle_id)}".

Quy tắc chung bắt buộc:
{circle.get('selection_prompt', '')}

Yêu cầu nội dung bắt buộc của Circle:
{circle.get('content_prompt', '')}

Tiêu chí thẩm định tiếp theo: chỉ bài đạt tổng điểm từ {_publish_threshold(circle)}/100
và không vi phạm phạm vi/điều kiện loại bỏ mới được viết. Bước này phải dùng đúng
tiêu chí đó để chọn ứng viên có khả năng đạt; nguồn chính thống hay khớp vài từ khóa
không đủ nếu bài ít giá trị cho độc giả. Nếu dữ liệu nguồn còn ngắn, không suy diễn.

Danh sách tin ứng viên:
{json.dumps(compact_candidates, ensure_ascii=False)}

Chọn đúng 1 tin nổi bật nhất nhưng vẫn phải thực sự phù hợp. Ưu tiên tin mới, nguồn uy tín,
sự kiện đang được quan tâm và có giá trị chia sẻ. Không chọn tin liên quan mơ hồ, lệch chủ đề
hoặc chọn cho đủ số lượng. TUYỆT ĐỐI không chọn lại cùng sự kiện, cùng câu chuyện, cùng trận đấu,
cùng mẹo hoặc cùng địa điểm đã đăng chỉ vì nguồn hay cách đặt tiêu đề khác nhau.
Trả về JSON thuần:
{{
  "matches": [
    {{"article_id": 123, "relevance_score": 85, "reason": "lý do ngắn gọn"}}
  ]
}}
Chỉ chọn tin có relevance_score từ 60 trở lên. Nếu không có tin phù hợp, trả về matches rỗng.
""".strip()

    ai_config = {}
    try:
        with open(AUTOMATION_CONFIG, encoding="utf-8") as file:
            ai_config = json.load(file).get("ai", {})
    except (OSError, ValueError, TypeError):
        pass

    try:
        result = generate_json(
            prompt,
            temperature=0.1,
            purpose=f"phân loại tin cho {circle.get('circle_name', circle_id)}",
            preferred_model=ai_config.get("model_text"),
        )
    except Exception as exc:
        logger.error(f"Không thể quét tin cho Circle {circle_id}: {exc}")
        saved, fallback_match = _save_first_local_match(circle_id, circle, candidates)
        if fallback_match:
            return {
                "success": True,
                "matched": saved,
                "evaluated_matches": 1,
                "selected_article_id": fallback_match["article_id"],
                "message": (
                    f"Gemini đang bận; bộ lọc dự phòng đã thêm {saved} tin phù hợp "
                    "vào hàng chờ của Circle."
                ),
                "circle_id": circle_id,
                "selection_mode": "local_fallback",
                "warning": "Kết quả được lọc theo từ khóa trên máy và nên được duyệt kỹ trước khi tạo bài.",
            }
        existing_matches = len(get_circle_article_queue(circle_id, status="MATCHED", limit=100))
        if existing_matches:
            message = (
                f"Không có tin mới để thêm. Circle hiện đã có {existing_matches} tin phù hợp "
                "đang chờ tạo bài hoặc duyệt."
            )
        else:
            message = f"Đã kiểm tra {len(candidates)} tin nhưng chưa có tin nào đạt đúng điều kiện của Circle."
        return {
            "success": True,
            "matched": 0,
            "message": message,
            "circle_id": circle_id,
            "selection_mode": "local_fallback",
            "existing_matches": existing_matches,
            "warning": "Gemini đang bận; kết quả được kiểm tra bằng bộ lọc chủ đề trên máy.",
        }

    valid_ids = {item["id"] for item in ai_candidates}
    matches = []
    seen = set()
    for item in result.get("matches", []) if isinstance(result, dict) else []:
        try:
            article_id = int(item.get("article_id") or 0)
            score = int(item.get("relevance_score") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if article_id not in valid_ids or article_id in seen or score < 60:
            continue
        seen.add(article_id)
        matches.append({
            "article_id": article_id,
            "relevance_score": max(0, min(100, score)),
            "reason": str(item.get("reason", ""))[:1000],
        })
        if len(matches) >= max_matches:
            break

    if not matches:
        saved, fallback_match = _save_first_local_match(circle_id, circle, candidates)
        if fallback_match:
            return {
                "success": True,
                "matched": saved,
                "evaluated_matches": 1,
                "selected_article_id": fallback_match["article_id"],
                "message": "Gemini không chọn được; bộ lọc chủ đề chặt trên máy đã chọn một tin phù hợp.",
                "circle_id": circle_id,
                "selection_mode": "local_after_ai_empty",
                "warning": "Ứng viên đã vượt bộ lọc bắt buộc của Circle nhưng vẫn được thẩm định lại trước khi đăng.",
            }
        existing_matches = len(get_circle_article_queue(circle_id, status="MATCHED", limit=100))
        return {
            "success": True,
            "matched": 0,
            "message": (
                f"Không có tin mới để thêm. Circle hiện đã có {existing_matches} tin phù hợp đang chờ xử lý."
                if existing_matches
                else "Chưa có tin nào đủ phù hợp với prompt của Circle."
            ),
            "existing_matches": existing_matches,
            "circle_id": circle_id,
        }

    saved = save_circle_article_matches(circle_id, matches)
    if not saved:
        # Ứng viên Gemini có thể vừa bị tác vụ Circle khác giữ, từng được tạo,
        # hoặc xung đột hàng đợi. Không dừng cả lượt: chuyển sang ứng viên cục bộ
        # hợp lệ kế tiếp để nút quét riêng vẫn tạo được bài.
        fallback_saved, fallback_match = _save_first_local_match(circle_id, circle, candidates)
        if fallback_match:
            return {
                "success": True,
                "matched": fallback_saved,
                "evaluated_matches": 1,
                "selected_article_id": fallback_match["article_id"],
                "message": "Ứng viên AI đang bận ở hàng khác; đã chuyển sang tin phù hợp kế tiếp.",
                "circle_id": circle_id,
                "selection_mode": "local_after_queue_conflict",
            }
        return {
            "success": True,
            "matched": 0,
            "evaluated_matches": len(matches),
            "message": "Tin AI chọn không còn khả dụng; chưa tìm được ứng viên thay thế.",
            "circle_id": circle_id,
            "selection_mode": "ai_queue_conflict",
        }
    return {
        "success": True,
        "matched": saved,
        "evaluated_matches": len(matches),
        "selected_article_id": matches[0]["article_id"],
        "message": f"Đã thêm {saved} tin phù hợp vào hàng chờ của Circle.",
        "circle_id": circle_id,
    }
