"""
review_ui/app.py
Giao dien Web quan tri & duyet bai viet UCircle AI Content Automation
- Xem danh sach bai viet trong Hang doi (QUEUED)
- Xem danh sach bai viet AI da tao (DRAFT, READY, REVIEW, PUBLISHED)
- Kich hoat thu thap ngay (Trigger Collect)
- Kich hoat phan tich & dang ngay (Trigger Publish)
- Phe duyet va dang thu cong
"""

import os
import sys
import json
import html
import asyncio
import threading
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env", override=True)

from src.storage import (
    get_connection,
    get_posts_by_status,
    get_post_by_id,
    update_post_status,
    get_recent_run_logs,
    get_queued_article_count,
    get_queue_stats,
    _safe_parse_list,
    count_published_today,
    count_published_this_hour,
    init_database,
    recover_stale_processing_articles,
    claim_article_for_processing,
    claim_circle_article_match,
    get_circle_article_queue,
    keep_only_best_circle_match,
    update_circle_article_match_status,
    update_post_full_content,
    update_article_source_url,
    published_source_url_exists,
    update_article_description,
    update_article_status,
    get_latest_image_asset,
)
from src.image_context import current_image_is_approved
from src.image_workflow import complete_post_image, manually_approve_post_image
from src.publisher import get_last_publish_error, publish_post
from src.ucircle_account import (
    account_manager,
    get_circle_config,
    get_discovered_circles,
    get_scan_status,
    scan_circles,
    set_circle_dashboard_visibility,
    update_circle_settings,
)
from src.logger import get_logger
from src.prompt_profiles import load_prompt_profiles
from src.facebook_auth import (
    complete_login as complete_facebook_login,
    create_login_url as create_facebook_login_url,
    disconnect as disconnect_facebook,
    status as facebook_auth_status,
)

logger = get_logger("review_ui")

app = FastAPI(title="UCircle AI Automation — Review Dashboard")

init_database()
_recovered_on_startup = recover_stale_processing_articles(stale_minutes=30)
if _recovered_on_startup:
    logger.warning(f"Đã khôi phục {_recovered_on_startup} bài bị kẹt PROCESSING về QUEUED.")

# Mount static & images
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "review_ui" / "static")), name="static")
app.mount("/images", StaticFiles(directory=str(ROOT_DIR / "data" / "images")), name="images")

templates = Jinja2Templates(directory=str(ROOT_DIR / "review_ui" / "templates"))

# State tracker for running background tasks
_bg_status = {"collecting": False, "publishing": False, "queue_publish": False}
_bg_result = {"collecting": None, "publishing": None}
QUEUE_CHECKPOINT_PATH = ROOT_DIR / "data" / "publish_queue_checkpoint.json"
PUBLISH_HISTORY_PATH = ROOT_DIR / "data" / "publish_queue_history.json"
AUTOMATION_CONFIG_PATH = ROOT_DIR / "config" / "automation.json"
_queue_checkpoint_lock = threading.Lock()
_publish_history_lock = threading.Lock()
_automation_config_lock = threading.Lock()
_queue_publish_pause = False
_queue_publish_resume_event = threading.Event()
_queue_publish_resume_event.set()
_queue_publish_progress = {
    "mode": "independent_circle_v1",
    "run_id": "",
    "started_at": "",
    "ended_at": "",
    "current": 0,
    "total": 0,
    "interval_sec": 60,
    "cycle": 0,
    "stage": "idle",
    "state": "idle",
    "completed": 0,
    "succeeded": 0,
    "failed": 0,
    "attempt": 0,
    "pending": 0,
    "paused": False,
    "selected_circle_ids": [],
    "pending_circle_ids": [],
    "successful_circle_ids": [],
    "circle_statuses": {},
    "remaining_sec": 0,
    "published_post_ids": [],
    "log": [],
}


def _read_automation_config() -> dict:
    try:
        with open(AUTOMATION_CONFIG_PATH, encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _set_publish_browser_hidden(hidden: bool) -> None:
    """Lưu nguyên tử cấu hình trình duyệt, không làm mất các mục cấu hình khác."""
    with _automation_config_lock:
        config = _read_automation_config()
        browser = config.get("browser") if isinstance(config.get("browser"), dict) else {}
        browser["publish_headless"] = bool(hidden)
        config["browser"] = browser
        temporary_path = AUTOMATION_CONFIG_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(AUTOMATION_CONFIG_PATH)


def _save_publish_checkpoint() -> None:
    """Ghi tiến trình tự động xuống đĩa để có thể tiếp tục sau khi dừng/restart."""
    snapshot = dict(_queue_publish_progress)
    QUEUE_CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = QUEUE_CHECKPOINT_PATH.with_suffix(".tmp")
    with _queue_checkpoint_lock:
        temp_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(QUEUE_CHECKPOINT_PATH)


def _load_publish_checkpoint() -> dict:
    """Đọc checkpoint hợp lệ; lỗi file không được làm Dashboard ngừng chạy."""
    try:
        data = json.loads(QUEUE_CHECKPOINT_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _load_publish_history() -> list[dict]:
    """Đọc lịch sử tiến trình; file hỏng không được làm Dashboard ngừng chạy."""
    try:
        data = json.loads(PUBLISH_HISTORY_PATH.read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _archive_publish_progress(progress: dict) -> None:
    """Thêm hoặc cập nhật một phiên đã dừng/hoàn tất vào lịch sử lâu dài."""
    if not isinstance(progress, dict) or not progress.get("selected_circle_ids"):
        return
    if progress.get("state") not in {"stopped", "finished"}:
        return
    run_id = str(progress.get("run_id") or progress.get("started_at") or "").strip()
    if not run_id:
        return
    record = {
        "run_id": run_id,
        "started_at": str(progress.get("started_at") or ""),
        "ended_at": str(progress.get("ended_at") or datetime.now().isoformat(timespec="seconds")),
        "state": str(progress.get("state") or "stopped"),
        "cycle": int(progress.get("cycle", 0) or 0),
        "attempt": int(progress.get("attempt", 0) or 0),
        "total": int(progress.get("total", 0) or 0),
        "succeeded": int(progress.get("succeeded", 0) or 0),
        "failed": int(progress.get("failed", 0) or 0),
        "pending": int(progress.get("pending", 0) or 0),
        "selected_circle_ids": list(progress.get("selected_circle_ids") or []),
        "successful_circle_ids": list(progress.get("successful_circle_ids") or []),
        "pending_circle_ids": list(progress.get("pending_circle_ids") or []),
        "circle_statuses": dict(progress.get("circle_statuses") or {}),
        "published_post_ids": list(progress.get("published_post_ids") or [])[-200:],
        "log": list(progress.get("log") or [])[-300:],
    }
    PUBLISH_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _publish_history_lock:
        history = _load_publish_history()
        history = [item for item in history if item.get("run_id") != run_id]
        history.append(record)
        history.sort(key=lambda item: str(item.get("ended_at") or ""), reverse=True)
        history = history[:50]
        temporary_path = PUBLISH_HISTORY_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(history, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(PUBLISH_HISTORY_PATH)


_saved_publish_checkpoint = _load_publish_checkpoint()
if _saved_publish_checkpoint.get("selected_circle_ids"):
    _queue_publish_progress.update(_saved_publish_checkpoint)
    _queue_publish_progress["mode"] = "independent_circle_v1"
    if not str(_queue_publish_progress.get("run_id") or "").strip():
        try:
            legacy_started_at = datetime.fromtimestamp(
                QUEUE_CHECKPOINT_PATH.stat().st_mtime
            ).isoformat(timespec="seconds")
        except OSError:
            legacy_started_at = datetime.now().isoformat(timespec="seconds")
        _queue_publish_progress["run_id"] = (
            "legacy_" + legacy_started_at.replace("-", "").replace(":", "").replace("T", "_")
        )
        _queue_publish_progress["started_at"] = str(
            _queue_publish_progress.get("started_at") or legacy_started_at
        )
        _queue_publish_progress.setdefault("log", []).append(
            "🗂 Đã chuyển checkpoint cũ vào hệ thống lịch sử phiên chạy."
        )
    if _queue_publish_progress.get("state") in {"running", "pausing"}:
        _queue_publish_progress["state"] = "stopped"
        _queue_publish_progress["stage"] = "stopped"
        _queue_publish_progress["paused"] = False
        _queue_publish_progress.setdefault("log", []).append(
            "♻️ Máy chủ đã khởi động lại; checkpoint được giữ để bấm Tiếp tục."
        )
        _queue_publish_progress["ended_at"] = datetime.now().isoformat(timespec="seconds")
        _save_publish_checkpoint()
    _archive_publish_progress(_queue_publish_progress)


def _run_collection_worker():
    _bg_status["collecting"] = True
    try:
        from collection_pipeline import run_collection
        run_collection()
        _bg_result["collecting"] = {"success": True, "message": "Quét RSS hoàn tất."}
    except Exception as e:
        logger.error(f"Lỗi khi chạy collection background: {e}")
        _bg_result["collecting"] = {"success": False, "message": str(e)}
    finally:
        _bg_status["collecting"] = False


def _run_publish_worker():
    _bg_status["publishing"] = True
    try:
        enabled_circle_ids = {
            item.get("local_circle_id")
            for item in get_discovered_circles()
            if item.get("publish_enabled")
        }
        ready_posts = [
            post for post in get_posts_by_status("READY")
            if post.get("circle_id") in enabled_circle_ids
        ]
        if not ready_posts:
            _bg_result["publishing"] = {"success": False, "message": "Không có bài READY đang chờ đăng."}
            return
        post_id = ready_posts[-1]["id"]
        success = publish_post(post_id, max_retries=1, manual_override=True)
        error_message = get_last_publish_error(post_id)
        _bg_result["publishing"] = {
            "success": success,
            "message": f"Đã đăng bài #{post_id} lên UCircle." if success else error_message,
        }
    except Exception as e:
        logger.error(f"Lỗi khi chạy publish background: {e}")
        _bg_result["publishing"] = {"success": False, "message": str(e)}
    finally:
        _bg_status["publishing"] = False


def evaluate_post_eligibility(post: dict) -> dict:
    """Đánh giá toàn diện bài viết để xem đủ điều kiện đăng lên UCircle hay không."""
    config_path = ROOT_DIR / "config" / "automation.json"
    cfg = {}
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        pass

    ai_cfg = cfg.get("ai", {})
    min_score = ai_cfg.get("min_publish_score", 82)
    sched = cfg.get("publish_schedule", {})
    start_h = sched.get("active_hours_start", 6)
    end_h = sched.get("active_hours_end", 23)
    max_day = sched.get("max_posts_per_day", 20)
    max_hour = sched.get("max_posts_per_hour", 4)

    checklist = []
    issues = []

    # 1. Nguồn bài viết
    source_name = (post.get("source_name") or "").strip()
    source_url = (post.get("source_url") or "").strip()
    is_source_ok = bool(source_name and (source_url.startswith("http://") or source_url.startswith("https://")))
    checklist.append({
        "category": "Nguồn gốc",
        "title": "Nguồn tin tức chính thống & URL",
        "passed": is_source_ok,
        "detail": f"{source_name} ({source_url[:35]}...)" if is_source_ok else "Thiếu tên nguồn hoặc URL không hợp lệ",
        "severity": "critical"
    })
    if not is_source_ok:
        issues.append("Chưa có nguồn tin hoặc URL bài gốc hợp lệ")

    # 2. Tiêu đề
    title = (post.get("ai_title") or "").strip()
    title_len = len(title)
    is_title_ok = 10 <= title_len <= 200
    checklist.append({
        "category": "Nội dung",
        "title": "Độ dài tiêu đề bài viết",
        "passed": is_title_ok,
        "detail": f"{title_len} ký tự (Yêu cầu: 10 - 200 ký tự)",
        "severity": "critical"
    })
    if not is_title_ok:
        issues.append(f"Tiêu đề không đạt độ dài chuẩn ({title_len} ký tự)")

    # 3. Tóm tắt nội dung
    summary = (post.get("ai_summary") or "").strip()
    words_count = len(summary.split())
    is_summary_ok = 40 <= words_count <= 400
    checklist.append({
        "category": "Nội dung",
        "title": "Độ dài tóm tắt bài viết",
        "passed": is_summary_ok,
        "detail": f"{words_count} từ (Yêu cầu: 40 - 400 từ, tối ưu 80 - 250 từ)",
        "severity": "critical"
    })
    if not is_summary_ok:
        issues.append(f"Độ dài tóm tắt ({words_count} từ) không đạt chuẩn")

    # 4. Điểm chính (Bullet points)
    bps = post.get("bullet_points") or []
    if isinstance(bps, str):
        bps = _safe_parse_list(bps)
    is_bp_ok = len(bps) >= 2
    checklist.append({
        "category": "Nội dung",
        "title": "Điểm chính (Bullet points)",
        "passed": is_bp_ok,
        "detail": f"{len(bps)} ý chính (Yêu cầu tối thiểu 2 ý)",
        "severity": "warning"
    })
    if not is_bp_ok:
        issues.append("Cần tối thiểu 2 điểm chính cho bài viết")

    # 5. Hashtags
    tags = post.get("hashtags") or []
    if isinstance(tags, str):
        tags = _safe_parse_list(tags)
    is_tags_ok = len(tags) == 5
    checklist.append({
        "category": "Nội dung",
        "title": "Thẻ Hashtags phân loại",
        "passed": is_tags_ok,
        "detail": f"{len(tags)}/5 hashtags ({', '.join(tags[:5])})",
        "severity": "warning"
    })
    if not is_tags_ok:
        issues.append("Bài viết cần đúng 5 hashtag liên quan nhất")

    # 6. Ảnh AI minh họa
    img_name = post.get("file_name") or ""
    img_path = post.get("image_path") or ""
    img_exists = False
    if img_name:
        disk_path = ROOT_DIR / "data" / "images" / img_name
        if disk_path.exists():
            img_exists = True
    if not img_exists and img_path:
        if Path(img_path).exists():
            img_exists = True

    checklist.append({
        "category": "Đa phương tiện",
        "title": "Ảnh bài viết" if post.get("is_ai_generated") == 0 else "Ảnh AI minh họa",
        "passed": current_image_is_approved(post, get_circle_config(post.get("circle_id")) or {}),
        "detail": (
            f"Đã có ảnh từ bài gốc ({img_name or 'file OK'})"
            if img_exists and post.get("is_ai_generated") == 0
            else (f"Đã có ảnh AI ({img_name or 'file OK'})" if img_exists else "Chưa có file ảnh trên đĩa — bài bị chặn đăng")
        ),
        "severity": "critical"
    })

    if not current_image_is_approved(post, get_circle_config(post.get("circle_id")) or {}):
        issues.append(post.get("image_validation_reason") or "Ảnh chưa được kiểm tra với phiên bản bài hiện tại")

    # 7. Điểm thẩm định chất lượng AI
    score = post.get("confidence_score", 0)
    is_score_ok = score >= min_score
    checklist.append({
        "category": "Thẩm định AI",
        "title": "Điểm chất lượng nội dung",
        "passed": is_score_ok,
        "detail": f"{score}/100 điểm (Ngưỡng tối thiểu: {min_score})",
        "severity": "critical"
    })
    if not is_score_ok:
        issues.append(f"Điểm thẩm định ({score}/100) chưa đạt mức tối thiểu ({min_score})")

    # 8. Trạng thái bài viết
    status = post.get("status", "DRAFT")
    checklist.append({
        "category": "Trạng thái",
        "title": "Trạng thái phê duyệt",
        "passed": status in ["READY", "PUBLISHED"],
        "detail": f"Hiện tại: {status} (Sẵn sàng đăng: READY)",
        "severity": "warning"
    })

    # 9. Session đăng nhập UCircle
    session_file = ROOT_DIR / "session.json"
    alt_session = Path(r"D:\Auto_tool\Fb-To-Ucricle\session.json")
    session_ok = (session_file.exists() and session_file.stat().st_size > 100) or (alt_session.exists() and alt_session.stat().st_size > 100)
    checklist.append({
        "category": "Hệ thống",
        "title": "Phiên đăng nhập UCircle (Session)",
        "passed": session_ok,
        "detail": "session.json đã sẵn sàng" if session_ok else "Chưa đăng nhập! Hãy dùng khối Tài khoản UCircle trên dashboard",
        "severity": "critical"
    })
    if not session_ok:
        issues.append("Chưa có phiên đăng nhập UCircle (hãy đăng nhập trên dashboard)")

    # 10. Khung giờ hoạt động
    cur_hour = datetime.now().hour
    is_active_hour = start_h <= cur_hour < end_h
    checklist.append({
        "category": "Vận hành",
        "title": "Khung giờ xuất bản",
        "passed": is_active_hour,
        "detail": f"Hiện tại: {cur_hour}h (Khung giờ hoạt động: {start_h}h - {end_h}h)",
        "severity": "info"
    })

    # 11. Hạn mức bài đăng (Rate limit)
    circle_id = post.get("circle_id", "circle_tintuc24h")
    count_day = count_published_today(circle_id)
    count_hour = count_published_this_hour(circle_id)
    day_ok = max_day is None or int(max_day) <= 0 or count_day < int(max_day)
    hour_ok = count_hour < max_hour
    rate_ok = day_ok and hour_ok
    checklist.append({
        "category": "Vận hành",
        "title": "Hạn mức đăng bài",
        "passed": rate_ok,
        "detail": (
            f"Hôm nay: {count_day} bài (không giới hạn theo ngày) | Giờ này: {count_hour}/{max_hour} bài"
            if max_day is None or int(max_day) <= 0
            else f"Hôm nay: {count_day}/{max_day} bài | Giờ này: {count_hour}/{max_hour} bài"
        ),
        "severity": "info"
    })
    if not day_ok:
        issues.append(f"Đã đạt giới hạn bài trong ngày ({count_day}/{max_day})")
    if not hour_ok:
        issues.append(f"Đã đạt giới hạn bài trong giờ này ({count_hour}/{max_hour})")

    # Tổng kết điều kiện
    critical_failed = [c for c in checklist if not c["passed"] and c["severity"] == "critical"]
    is_eligible = len(critical_failed) == 0

    if is_eligible and status == "READY":
        verdict = "ELIGIBLE"
        verdict_text = "ĐỦ ĐIỀU KIỆN ĐĂNG NGAY"
        verdict_class = "eligible"
        verdict_desc = "Bài viết đáp ứng đầy đủ tất cả tiêu chuẩn chất lượng, nguồn gốc và tài khoản UCircle."
    elif is_eligible:
        verdict = "NEEDS_APPROVAL"
        verdict_text = "ĐẠT TIÊU CHUẨN (CẦN DUYỆT)"
        verdict_class = "warning"
        verdict_desc = f"Nội dung đạt chuẩn nhưng đang ở trạng thái '{status}'. Bạn có thể bấm Duyệt (Ready) hoặc Đăng ngay."
    else:
        verdict = "INELIGIBLE"
        verdict_text = "CHƯA ĐỦ ĐIỀU KIỆN"
        verdict_class = "ineligible"
        verdict_desc = f"Còn {len(issues)} vấn đề cần lưu ý: {', '.join(issues)}"

    return {
        "is_eligible": is_eligible,
        "verdict": verdict,
        "verdict_text": verdict_text,
        "verdict_class": verdict_class,
        "verdict_desc": verdict_desc,
        "score": score,
        "checklist": checklist,
        "issues": issues,
        "stats": {
            "title_length": title_len,
            "word_count": words_count,
            "bullet_count": len(bps),
            "hashtag_count": len(tags),
            "has_image": img_exists,
            "image_name": img_name,
            "published_today": count_day,
            "published_this_hour": count_hour,
            "active_hour": is_active_hour
        }
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, status: Optional[str] = None, tab: Optional[str] = "posts"):
    conn = get_connection()
    try:
        # Posts list
        visible_statuses = {"DRAFT", "REVIEW", "READY", "FAILED", "WAITING_IMAGE"}
        if status and status in visible_statuses:
            posts = get_posts_by_status(status)
        else:
            rows = conn.execute("""
                SELECT p.*, m.file_path as image_path, m.image_prompt, m.file_name,
                       m.is_ai_generated, m.source_url as image_source_url,
                       m.source_name as image_source_name,
                       m.validation_status as image_validation_status, m.context_hash as image_context_hash,
                       m.provider as image_provider, m.model as image_model,
                       m.validation_reason as image_validation_reason, m.relevance_score as image_relevance_score
                FROM generated_posts p
                LEFT JOIN media_assets m ON m.id = (
                    SELECT MAX(m2.id) FROM media_assets m2
                    WHERE m2.post_id = p.id AND m2.is_active = 1
                )
                WHERE p.status IN ('DRAFT', 'REVIEW', 'READY', 'FAILED', 'WAITING_IMAGE')
                ORDER BY p.id DESC LIMIT 100
            """).fetchall()
            posts = []
            for r in rows:
                d = dict(r)
                d["bullet_points"] = _safe_parse_list(d.get("bullet_points"))
                d["hashtags"] = _safe_parse_list(d.get("hashtags"))
                posts.append(d)

        # Queued articles (chỉ lấy 30 bài HOT nhất hiển thị)
        q_rows = conn.execute("""
            SELECT a.id, a.original_title, a.source_url, a.published_at, a.collected_at,
                   s.name as source_name, s.trust_level
            FROM articles a
            JOIN sources s ON a.source_id = s.id
            WHERE a.status = 'QUEUED'
            ORDER BY s.trust_level DESC, a.published_at DESC
            LIMIT 30
        """).fetchall()
        queued_articles = [dict(r) for r in q_rows]

        # Counts
        queued_count = get_queued_article_count()
        post_counts = {}
        for r in conn.execute("SELECT status, count(*) FROM generated_posts GROUP BY status").fetchall():
            post_counts[r[0]] = r[1]
        total_posts_count = sum(post_counts.get(item, 0) for item in visible_statuses)

        run_logs = get_recent_run_logs(limit=5)
    finally:
        conn.close()

    circles_config = {}
    try:
        with open(ROOT_DIR / "config" / "circles.json", encoding="utf-8") as f:
            circle_ids = [
                circle.get("circle_id")
                for circle in json.load(f).get("circles", [])
                if circle.get("circle_id")
            ]
            circles_config = {
                circle_id: get_circle_config(circle_id) for circle_id in circle_ids
            }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Không đọc được tên Circle để hiển thị trên bài viết.")
    for post in posts:
        circle = circles_config.get(post.get("circle_id"), {})
        post["circle_name"] = circle.get("circle_name") or post.get("circle_id")
        post["image_current"] = current_image_is_approved(post, circle)

    circle_groups = []
    discovered = get_discovered_circles()
    publish_enabled_ids = {
        item.get("local_circle_id") for item in discovered if item.get("publish_enabled")
    }
    eligible_ready_count = sum(
        1 for post in get_posts_by_status("READY")
        if post.get("circle_id") in publish_enabled_ids
    )
    known_ids = set()
    hidden_circles = []
    for discovered_circle in discovered:
        circle_id = discovered_circle.get("local_circle_id")
        if not circle_id:
            continue
        known_ids.add(circle_id)
        circle = dict(circles_config.get(circle_id, {}))
        circle.update({
            "circle_id": circle_id,
            "circle_name": circle.get("display_name") or discovered_circle.get("circle_name") or circle.get("circle_name") or circle_id,
            "category_name": discovered_circle.get("category_name") or discovered_circle.get("format") or "Chưa phân loại",
            "scan_enabled": bool(discovered_circle.get("scan_enabled", circle.get("scan_enabled", False))),
            "publish_enabled": bool(discovered_circle.get("publish_enabled", circle.get("publish_enabled", False))),
        })
        if not circle.get("dashboard_visible", True):
            hidden_circles.append(circle)
            continue
        group_posts = [post for post in posts if post.get("circle_id") == circle_id]
        circle_groups.append({
            "circle": circle,
            "posts": group_posts,
            "matched_articles": get_circle_article_queue(circle_id),
            "ready_count": sum(1 for post in group_posts if post.get("status") == "READY"),
        })
    for circle_id, circle in circles_config.items():
        if circle_id in known_ids:
            continue
        group_posts = [post for post in posts if post.get("circle_id") == circle_id]
        if group_posts:
            circle_groups.append({
                "circle": circle,
                "posts": group_posts,
                "matched_articles": get_circle_article_queue(circle_id),
                "ready_count": sum(1 for post in group_posts if post.get("status") == "READY"),
            })

    min_publish_interval = 30
    publish_browser_hidden = True
    facebook_status = facebook_auth_status()
    media_source_status = {
        "youtube_ready": bool(os.getenv("YOUTUBE_API_KEY", "").strip()),
        "facebook_credentials_ready": facebook_status["credentials_ready"],
        "facebook_ready": facebook_status["connected"],
        "facebook_page_count": facebook_status["page_count"],
    }
    try:
        with open(ROOT_DIR / "config" / "media_sources.json", encoding="utf-8") as f:
            media_config = json.load(f)
        facebook_pages = media_config.get("facebook", {}).get("pages", [])
        configured_page_count = len(facebook_pages) if isinstance(facebook_pages, list) else 0
        if not media_source_status["facebook_ready"]:
            media_source_status["facebook_page_count"] = configured_page_count
            media_source_status["facebook_ready"] = bool(
                os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
                and configured_page_count
            )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Không đọc được cấu hình nguồn media.")
    try:
        automation_config = _read_automation_config()
        min_publish_interval = max(
            0,
            int(automation_config.get("publish_schedule", {}).get("min_interval_minutes", 30) or 0),
        )
        publish_browser_hidden = bool(
            automation_config.get("browser", {}).get("publish_headless", True)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Không đọc được min_interval_minutes; dùng mặc định 30 phút.")

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "posts": posts,
        "circle_groups": circle_groups,
        "hidden_circles": hidden_circles,
        "eligible_ready_count": eligible_ready_count,
        "queued_articles": queued_articles,
        "queued_count": queued_count,
        "post_counts": post_counts,
        "total_posts_count": total_posts_count,
        "run_logs": run_logs,
        "current_status": status or "ALL",
        "tab": tab or "posts",
        "bg_status": _bg_status,
        "min_publish_interval": min_publish_interval,
        "publish_browser_hidden": publish_browser_hidden,
        "media_source_status": media_source_status,
        "prompt_profiles": load_prompt_profiles().get("profiles", []),
        "image_settings": _read_automation_config().get("images", {}),
    })


@app.get("/api/browser/settings")
async def get_browser_settings():
    config = _read_automation_config()
    hidden = bool(config.get("browser", {}).get("publish_headless", True))
    return {
        "success": True,
        "publish_browser_hidden": hidden,
        "login_browser_visible": True,
    }


@app.post("/api/browser/settings")
async def update_browser_settings(request: Request):
    data = await request.json()
    hidden = data.get("publish_browser_hidden")
    if not isinstance(hidden, bool):
        return JSONResponse(
            {"success": False, "error": "publish_browser_hidden phải là true hoặc false."},
            status_code=422,
        )
    try:
        _set_publish_browser_hidden(hidden)
    except OSError as exc:
        logger.error(f"Không lưu được cấu hình trình duyệt: {exc}")
        return JSONResponse(
            {"success": False, "error": "Không lưu được cấu hình trình duyệt."},
            status_code=500,
        )
    return {
        "success": True,
        "publish_browser_hidden": hidden,
        "message": (
            "Đã bật chế độ ẩn trình duyệt khi đăng tự động."
            if hidden else "Đã bật chế độ hiện trình duyệt khi đăng tự động."
        ),
    }


@app.get("/api/media/status")
async def get_media_source_status():
    """Trạng thái cấu hình nguồn media, không trả về khóa bí mật."""
    page_count = 0
    try:
        with open(ROOT_DIR / "config" / "media_sources.json", encoding="utf-8") as file:
            pages = json.load(file).get("facebook", {}).get("pages", [])
            page_count = len(pages) if isinstance(pages, list) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    facebook_status = facebook_auth_status()
    return {
        "youtube_ready": bool(os.getenv("YOUTUBE_API_KEY", "").strip()),
        "facebook_credentials_ready": facebook_status["credentials_ready"],
        "facebook_ready": bool(facebook_status["connected"] or (os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip() and page_count)),
        "facebook_page_count": facebook_status["page_count"] or page_count,
        "video_download_enabled": False,
        "video_reupload_enabled": False,
    }


@app.get("/api/images/settings")
async def get_image_settings():
    return {"success": True, "images": _read_automation_config().get("images", {}),
            "gemini_key_configured": bool(os.getenv("GEMINI_API_KEY", "").strip())}


@app.post("/api/images/settings")
async def save_image_settings(request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Cấu hình ảnh phải là một đối tượng JSON.")
    provider = data.get("provider")
    enabled = data.get("gemini_generation_enabled", False)
    model = data.get("gemini_model", "")
    if provider not in {"pollinations", "gemini"} or not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="Dịch vụ ảnh hoặc tùy chọn bật Gemini không hợp lệ.")
    if not isinstance(model, str) or not model.startswith("gemini-") or "image" not in model or len(model) > 100:
        raise HTTPException(status_code=422, detail="Tên model phải là model Gemini hỗ trợ sinh ảnh.")
    with _automation_config_lock:
        config = _read_automation_config()
        config.setdefault("images", {}).update({
            "provider": provider, "gemini_model": model.strip(), "gemini_generation_enabled": enabled,
        })
        temp_path = AUTOMATION_CONFIG_PATH.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(AUTOMATION_CONFIG_PATH)
    return {"success": True, "message": "Đã lưu dịch vụ ảnh. Gemini đọc toàn bài để thiết kế/kiểm tra ảnh.",
            "images": config["images"]}


@app.get("/api/facebook/status")
async def get_facebook_status():
    return facebook_auth_status()


@app.get("/api/facebook/login/start")
async def start_facebook_login():
    try:
        return RedirectResponse(create_facebook_login_url(), status_code=302)
    except Exception as exc:
        return HTMLResponse(
            f"<h2>Không thể mở đăng nhập Facebook</h2><p>{html.escape(str(exc))}</p>",
            status_code=400,
        )


@app.get("/api/facebook/oauth/callback")
async def facebook_oauth_callback(
    code: str = "", state: str = "", error: str = "", error_description: str = ""
):
    if error or error_description:
        message = f"Facebook từ chối đăng nhập: {error_description or error}"
        success = False
    else:
        try:
            result = await asyncio.to_thread(complete_facebook_login, code, state)
            message = f"Đã kết nối {result['page_count']} Facebook Page."
            success = True
        except Exception as exc:
            message = str(exc)
            success = False
    safe_message = html.escape(message)
    color = "#34d399" if success else "#f87171"
    return HTMLResponse(f"""
        <!doctype html><html lang="vi"><meta charset="utf-8">
        <body style="font-family:sans-serif;background:#0f172a;color:#e2e8f0;padding:32px">
          <h2 style="color:{color}">{'Kết nối thành công' if success else 'Kết nối thất bại'}</h2>
          <p>{safe_message}</p><p>Bạn có thể đóng cửa sổ này.</p>
          <script>if(window.opener){{window.opener.postMessage({{type:'facebook-auth-complete',success:{str(success).lower()}}}, window.location.origin);}} setTimeout(()=>window.close(),1800);</script>
        </body></html>
    """, status_code=200 if success else 400)


@app.post("/api/facebook/disconnect")
async def disconnect_facebook_account():
    disconnect_facebook()
    return {"success": True, "message": "Đã ngắt kết nối Facebook cục bộ."}


@app.post("/api/trigger/collect")
async def trigger_collect():
    if _bg_status["collecting"]:
        return JSONResponse({"status": "already_running", "message": "Thu thập đang chạy dở..."}, status_code=409)
    _bg_result["collecting"] = None
    _bg_status["collecting"] = True
    threading.Thread(target=_run_collection_worker, daemon=True).start()
    return {"status": "started", "message": "Đã bắt đầu thu thập RSS bài mới..."}


@app.post("/api/trigger/publish")
async def trigger_publish():
    if _bg_status["publishing"]:
        return JSONResponse({"status": "already_running", "message": "Tiến trình đăng bài đang chạy..."}, status_code=409)
    enabled_ids = {
        item.get("local_circle_id")
        for item in get_discovered_circles()
        if item.get("publish_enabled")
    }
    if not any(post.get("circle_id") in enabled_ids for post in get_posts_by_status("READY")):
        return JSONResponse({"status": "empty", "message": "Không có bài Chờ đăng thuộc Circle đã bật đăng bài."}, status_code=409)
    _bg_result["publishing"] = None
    _bg_status["publishing"] = True
    threading.Thread(target=_run_publish_worker, daemon=True).start()
    return {"status": "started", "message": "Đã bắt đầu đăng bài READY tiếp theo..."}


@app.get("/api/status")
async def get_system_status():
    return {
        "bg_status": _bg_status,
        "bg_result": _bg_result,
        "queued_count": get_queued_article_count()
    }


@app.get("/api/ucircle/account/status")
async def get_ucircle_account_status():
    """Trạng thái phiên đăng nhập và dữ liệu Circle đã quét."""
    return {
        "success": True,
        "account": account_manager.status(),
        "scan": get_scan_status(),
        "circles": get_discovered_circles(),
    }


@app.post("/api/ucircle/login/start")
async def start_ucircle_login():
    """Mở trình duyệt đăng nhập UCircle từ dashboard."""
    started, message = account_manager.start_login()
    return JSONResponse(
        {"success": started, "message": message, "account": account_manager.status()},
        status_code=202 if started else 409,
    )


@app.post("/api/ucircle/login/save")
async def save_ucircle_login():
    """Yêu cầu trình duyệt đăng nhập lưu session.json."""
    accepted, message = account_manager.request_save()
    return JSONResponse(
        {"success": accepted, "message": message},
        status_code=202 if accepted else 409,
    )


@app.post("/api/ucircle/login/cancel")
async def cancel_ucircle_login():
    """Hủy luồng đăng nhập UCircle đang mở."""
    accepted, message = account_manager.cancel_login()
    return JSONResponse(
        {"success": accepted, "message": message},
        status_code=202 if accepted else 409,
    )


@app.post("/api/ucircle/circles/scan")
async def scan_ucircle_account_circles():
    """Quét chỉ đọc các Circle hiển thị trong tài khoản UCircle."""
    try:
        circles = await asyncio.to_thread(scan_circles)
        return {
            "success": True,
            "message": f"Đã quét thấy {len(circles)} Circle bạn sở hữu.",
            "circles": circles,
        }
    except Exception as exc:
        logger.error(f"Lỗi quét Circle UCircle: {exc}", exc_info=True)
        return JSONResponse(
            {"success": False, "error": str(exc), "circles": get_discovered_circles()},
            status_code=400,
        )


@app.post("/api/ucircle/circles/{circle_id}/settings")
async def save_ucircle_circle_settings(circle_id: str, request: Request):
    """Lưu cờ quét/đăng và prompt riêng của một Circle sở hữu."""
    data = await request.json()
    try:
        circle = update_circle_settings(
            circle_id,
            scan_enabled=bool(data.get("scan_enabled", False)),
            publish_enabled=bool(data.get("publish_enabled", False)),
            content_prompt=str(data.get("content_prompt", "")),
            display_name=data.get("display_name"),
            media_platforms=data.get("media_platforms"),
            media_search_query=data.get("media_search_query"),
            prompt_profile_id=data.get("prompt_profile_id"),
            image_prompt=data.get("image_prompt"),
        )
        return {
            "success": True,
            "message": f"Đã lưu cấu hình Circle '{circle.get('circle_name', circle_id)}'.",
            "circle": circle,
        }
    except PermissionError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=403)
    except KeyError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=422)


@app.post("/api/ucircle/circles/{circle_id}/visibility")
async def change_ucircle_circle_visibility(circle_id: str, request: Request):
    """Ẩn/hiện Circle trên Dashboard, không xóa dữ liệu."""
    data = await request.json()
    try:
        circle = set_circle_dashboard_visibility(circle_id, bool(data.get("visible", True)))
        return {
            "success": True,
            "message": "Đã thêm Circle vào Dashboard." if circle.get("dashboard_visible") else "Đã ẩn Circle khỏi Dashboard; dữ liệu vẫn được giữ nguyên.",
            "circle": circle,
        }
    except PermissionError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=403)
    except KeyError as exc:
        return JSONResponse({"success": False, "error": str(exc)}, status_code=404)


@app.post("/api/ucircle/circles/{circle_id}/scan-articles")
async def scan_articles_for_ucircle(circle_id: str):
    """Chọn một tin nổi bật, tạo bài và đưa thẳng vào hàng Chờ đăng."""
    result = await asyncio.to_thread(_scan_and_generate_for_circle, circle_id)
    return JSONResponse(result, status_code=200 if result.get("success") else 400)


@app.post("/api/ucircle/circles/bulk-scan-articles")
async def scan_articles_for_multiple_ucircles(request: Request):
    """Mỗi Circle chọn một tin nổi bật và tự tạo một bài Chờ đăng."""

    data = await request.json()
    circle_ids = data.get("circle_ids", [])
    if not isinstance(circle_ids, list):
        return JSONResponse(
            {"success": False, "error": "circle_ids phải là một danh sách."},
            status_code=422,
        )
    circle_ids = [str(circle_id).strip() for circle_id in circle_ids if str(circle_id).strip()]
    if not circle_ids:
        return JSONResponse(
            {"success": False, "error": "Chưa chọn Circle nào để quét."},
            status_code=422,
        )
    if len(circle_ids) > 20:
        return JSONResponse(
            {"success": False, "error": "Mỗi lượt chỉ được quét tối đa 20 Circle."},
            status_code=422,
        )

    # Chỉ chạy tối đa hai luồng để nhanh hơn tuần tự nhưng không dồn quá nhiều
    # yêu cầu vào hạn mức Gemini miễn phí.
    semaphore = asyncio.Semaphore(2)

    async def run_one(circle_id: str) -> dict:
        async with semaphore:
            return await asyncio.to_thread(_scan_and_generate_for_circle, circle_id)

    results = await asyncio.gather(*(run_one(circle_id) for circle_id in circle_ids))
    succeeded = sum(1 for item in results if item.get("success"))
    generated = sum(1 for item in results if item.get("generation", {}).get("success"))
    result = {
        "success": succeeded == len(results),
        "message": (
            f"Đã xử lý {succeeded}/{len(results)} Circle và tự tạo {generated} bài Chờ đăng."
        ),
        "results": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "matched": sum(int(item.get("scan", {}).get("matched", 0) or 0) for item in results),
        "generated": generated,
        "media_saved": sum(int(item.get("media_collection", {}).get("saved", 0) or 0) for item in results),
        "max_workers": min(2, len(results)),
    }
    # Luôn trả 200 sau khi tác vụ đã chạy để Dashboard hiển thị lỗi riêng từng Circle.
    return JSONResponse(result, status_code=200)


def process_single_article(article_id: int, circle_id: str = "circle_tintuc24h", control=None) -> dict:
    """Xử lý 1 bài báo từ hàng đợi bằng AI (Analyzer -> Writer -> Image -> Validator) mà không publish ngay."""
    from src.analyzer import analyze_article
    from src.writer import write_post
    from src.storage import (
        get_connection, update_article_status, save_story, save_post, update_post_status
    )

    circle_config = get_circle_config(circle_id)
    if not circle_config:
        return {"success": False, "error": f"Không tìm thấy cấu hình Circle '{circle_id}'."}
    if not circle_config.get("ownership_verified"):
        return {"success": False, "error": "Circle chưa được xác minh quyền sở hữu."}
    if not circle_config.get("scan_enabled", False):
        return {"success": False, "error": "Circle chưa bật tùy chọn Quét bài."}

    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT a.*, s.name as source_name, s.trust_level
            FROM articles a
            JOIN sources s ON a.source_id = s.id
            WHERE a.id = ?
        """, (article_id,)).fetchone()
        if not row:
            return {"success": False, "error": f"Không tìm thấy article #{article_id}"}
        article = dict(row)
    finally:
        conn.close()

    if not claim_article_for_processing(article_id):
        return {"success": False, "error": "Bài này không còn ở hàng đợi hoặc đang được xử lý bởi tiến trình khác"}

    # Google News dùng URL trung gian chạy JavaScript. Giải sang trang báo thật
    # trước khi viết bài để cả liên kết nguồn và ảnh đại diện đều đúng.
    from src.source_resolver import resolve_source_url
    resolved_source_url = resolve_source_url(article.get("source_url", ""))
    if resolved_source_url and resolved_source_url != article.get("source_url"):
        if update_article_source_url(article_id, resolved_source_url):
            article["source_url"] = resolved_source_url
        else:
            if published_source_url_exists(resolved_source_url):
                update_article_status(article_id, "DUPLICATE")
                return {
                    "success": False,
                    "error": "Nguồn bài gốc đã được đăng trước đó; chuyển sang chọn tin khác.",
                }
            # URL thật có thể đã nằm ở một article cũ từng bị loại, nên cột UNIQUE
            # không cho ghi lại. Vẫn dùng URL thật trong bộ nhớ để đọc nội dung và
            # tạo bài; article hiện tại sẽ đổi trạng thái sau lượt này.
            article["source_url"] = resolved_source_url
            logger.info(
                f"Dùng URL báo gốc trong lượt hiện tại cho article #{article_id}; "
                "không ghi đè vì URL đã có ở article khác."
            )

    # RSS Google News thường chỉ có một câu ngắn. Đọc nội dung trang nguồn trước
    # khi phân tích để AI có đủ dữ kiện, bước thực hiện và bối cảnh để tóm tắt.
    if article.get("platform", "news") == "news" and len(
        str(article.get("original_description") or "").split()
    ) < 90:
        try:
            from src.source_content import fetch_source_description
            source_description = fetch_source_description(
                article.get("source_url", ""), expected_title=article.get("original_title", "")
            )
            if source_description and update_article_description(article_id, source_description):
                article["original_description"] = source_description
                logger.info(
                    f"Đã bổ sung {len(source_description.split())} từ từ trang nguồn cho article #{article_id}."
                )
        except Exception as exc:
            logger.warning(f"Không đọc được nội dung trang nguồn article #{article_id}: {exc}")

    # Analyzer phải dùng đúng tiêu chí của Circle đích. Trước đây prompt thẩm định
    # bị cố định ở "Tin Tức 24h", khiến bài ẩm thực, bóng đá... bị loại nhầm.
    analysis = analyze_article(article, circle=circle_config)
    if not analysis:
        update_article_status(article_id, "QUEUED")
        return {"success": False, "error": "Gemini Analyzer không phân tích được bài báo"}

    overall_score = analysis.get("overall_score", 0)
    if not analysis.get("publish", False):
        update_article_status(article_id, "REJECTED")
        return {
            "success": False,
            "error_code": "article_not_suitable",
            "error": f"Gemini đánh giá bài chưa phù hợp để đăng ({overall_score}/100): {analysis.get('reason', '')}",
        }
    post_data = write_post(article, analysis, circle_id=circle_id)
    if not post_data:
        update_article_status(article_id, "QUEUED")
        return {"success": False, "error": "Gemini Writer không tạo được bài viết"}

    source_name = article.get("source_name", "")
    post_data["source_url"] = article.get("source_url", "")
    post_data["source_name"] = source_name
    post_data["source_image_url"] = article.get("source_image_url", "")
    post_data["thumbnail_url"] = article.get("thumbnail_url", "")

    story_id = save_story({
        "topic": analysis.get("topic", "news"),
        "main_title": article.get("original_title", ""),
        "key_facts": analysis.get("key_facts", []),
        "confidence_score": overall_score,
        "sensitivity_level": analysis.get("sensitivity_level", "low"),
        "is_sensitive": analysis.get("is_sensitive", False),
        "status": "ANALYZED"
    })
    update_article_status(article_id, "PROCESSED", story_id=story_id)

    post_record = {
        "story_id": story_id,
        "article_id": article_id,
        "circle_id": circle_id,
        "ai_title": post_data.get("ai_title"),
        "ai_summary": post_data.get("ai_summary"),
        "bullet_points": post_data.get("bullet_points", []),
        "hashtags": post_data.get("hashtags", []),
        "source_name": source_name,
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
    image_result = complete_post_image(post_id, control=control)
    result = {
        **image_result,
        "post_id": post_id,
        "title": post_data.get("ai_title"),
        "circle_id": circle_id,
        "circle_name": circle_config.get("circle_name", circle_id),
        "analysis_mode": analysis.get("analysis_mode", "ai"),
        "generation_mode": post_data.get("generation_mode", "ai"),
        "warning": (
            "Gemini đang quá tải hoặc hết hạn mức; bài được tạo bằng bộ xử lý dự phòng."
            if analysis.get("analysis_mode") == "local_fallback" or post_data.get("generation_mode") == "local_fallback"
            else ""
        ),
    }
    return result


def _generate_matched_article(circle_id: str, article_id: int | None = None, control=None) -> dict:
    """Tạo đúng một bài từ hàng đợi riêng và tự chuyển sang READY."""
    matches = get_circle_article_queue(circle_id, status="MATCHED", limit=10)
    if article_id:
        match = next((item for item in matches if int(item["article_id"]) == int(article_id)), None)
    else:
        match = matches[0] if matches else None
    if not match:
        return {"success": False, "error": "Circle chưa có tin phù hợp mới để tạo bài."}

    selected_id = int(match["article_id"])
    if not claim_circle_article_match(circle_id, selected_id):
        return {"success": False, "error": "Tin đang được tiến trình khác xử lý."}

    try:
        result = process_single_article(selected_id, circle_id, control=control)
    except Exception as exc:
        # Không để một lỗi bất ngờ khóa vĩnh viễn cả article lẫn hàng Circle.
        update_article_status(selected_id, "QUEUED")
        update_circle_article_match_status(circle_id, selected_id, "MATCHED")
        logger.exception(f"Khôi phục article #{selected_id} sau lỗi tạo bài: {exc}")
        return {"success": False, "error": f"Lỗi tạo bài tạm thời: {str(exc)[:300]}"}
    if result.get("success") or result.get("status") == "WAITING_IMAGE":
        update_circle_article_match_status(circle_id, selected_id, "GENERATED")
        return result

    article_status = None
    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM articles WHERE id = ?", (selected_id,)).fetchone()
        article_status = row[0] if row else None
    finally:
        conn.close()
    if article_status == "REJECTED":
        # REJECTED ở đây chỉ có nghĩa là không phù hợp với Circle hiện tại.
        # Trả bài về kho chung để Circle khác vẫn có thể dùng, còn Circle này
        # ghi nhớ riêng để không chọn lặp lại cùng nguồn ở lần quét tiếp theo.
        update_article_status(selected_id, "QUEUED")
        update_circle_article_match_status(circle_id, selected_id, "REJECTED")
    else:
        update_circle_article_match_status(
            circle_id,
            selected_id,
            "SKIPPED" if article_status in {"PROCESSED", "DUPLICATE"} else "MATCHED",
        )
    return result


def _scan_and_generate_for_circle(circle_id: str, control=None) -> dict:
    """Thu thập, chọn một tin nổi bật và tự tạo một bài Chờ đăng."""
    from src.circle_selector import scan_articles_for_circle
    from src.media_collector import collect_media_for_circle

    circle = get_circle_config(circle_id) or {}
    circle_name = circle.get("display_name") or circle.get("circle_name") or circle_id
    waiting = get_posts_by_status("WAITING_IMAGE", circle_id=circle_id)
    if waiting:
        waiting_post = waiting[-1]
        result = complete_post_image(waiting_post["id"], control=control)
        if result.get("success"):
            return {**result, "circle_name": circle_name, "generated": 0, "resumed_image": True,
                    "message": "Đã hoàn tất ảnh và đưa bài sang Chờ đăng."}
        image_result = result.get("image") if isinstance(result.get("image"), dict) else {}
        no_usable_image_file = not image_result.get("file_path")
        if control is not None and not control():
            return {**result, "circle_name": circle_name, "generated": 0, "resumed_image": True,
                    "message": result.get("error", "Đã dừng; giữ nguyên bài để tiếp tục.")}
        # Ảnh bị validator từ chối hoặc hết lượt tạo ảnh mà vẫn không có file
        # là lỗi dứt điểm của ứng viên này; loại bài rồi tìm nguồn khác để tránh
        # auto-cycle lặp mãi cùng bài ở trạng thái WAITING_IMAGE.
        if (image_result.get("status") == "REJECT" or result.get("status") == "REJECTED"
                or no_usable_image_file):
            reason = str(result.get("error") or image_result.get("reason") or "Ảnh/nội dung không đạt tiêu chí.")
            update_post_status(waiting_post["id"], "REJECTED", reject_reason=reason)
            if waiting_post.get("article_id"):
                update_circle_article_match_status(
                    circle_id, int(waiting_post["article_id"]), "REJECTED"
                )
            logger.warning(
                f"{circle_name}: loại bài #{waiting_post['id']} khỏi luồng tự động "
                f"do ảnh hoặc nội dung bị từ chối: {reason}"
            )
        else:
            # Lỗi dịch vụ/xác minh tạm thời vẫn được giữ lại để tiếp tục sau.
            return {**result, "circle_name": circle_name, "generated": 0, "resumed_image": True,
                    "message": result.get("error", "Ảnh đang chờ được kiểm tra lại.")}
    if control is not None and not control():
        return {"success": False, "error": "Tiến trình đã dừng trước khi quét nguồn."}
    selection_attempts = []
    # Dọn các tác vụ đã chết từ tiến trình/reload trước để tin phù hợp được dùng lại.
    recover_stale_processing_articles(stale_minutes=15)
    try:
        media_result = collect_media_for_circle(circle_id)
    except Exception as exc:
        logger.warning(f"Không thu thập được media cho {circle_name}: {exc}")
        media_result = {"success": False, "saved": 0, "error": str(exc)[:500]}
    try:
        generation = {"success": False, "error": "Circle chưa có tin phù hợp mới để tạo bài."}
        scan_result = {"success": False, "matched": 0}
        # Một URL Google News khác có thể trỏ tới bài đã đăng hoặc ứng viên đầu
        # có thể không đạt thẩm định. Tự chuyển tối đa ba ứng viên trong cùng lần bấm.
        for _selection_attempt in range(3):
            scan_result = scan_articles_for_circle(
                circle_id,
                limit=12,
                max_matches=1,
                preferred_article_ids=media_result.get("candidate_ids") or [],
            )
            preferred_article_id = scan_result.get("selected_article_id")
            selected_article_id = keep_only_best_circle_match(circle_id, preferred_article_id)
            if not selected_article_id:
                generation = {
                    "success": False,
                    "error": scan_result.get("error") or scan_result.get("message")
                    or "Circle chưa có tin phù hợp mới để tạo bài.",
                }
                break
            generation = _generate_matched_article(circle_id, selected_article_id, control=control)
            selection_attempts.append({
                "article_id": selected_article_id,
                "success": bool(generation.get("success")),
                "reason": generation.get("error") or generation.get("title") or "",
            })
            if generation.get("success"):
                break
            # Ảnh nguồn không có và lượt tạo ảnh dự phòng cũng không tạo ra
            # tệp dùng được: loại ngay bài này để thử ứng viên tiếp theo trong
            # cùng lượt quét, thay vì trả nó về WAITING_IMAGE vô thời hạn.
            generation_image = generation.get("image") if isinstance(generation.get("image"), dict) else {}
            if (generation.get("status") == "WAITING_IMAGE"
                    and not generation_image.get("file_path")
                    and generation.get("post_id")):
                if control is not None and not control():
                    return {
                        "success": False,
                        "post_id": generation.get("post_id"),
                        "circle_id": circle_id,
                        "circle_name": circle_name,
                        "generated": 0,
                        "error": "Đã dừng; giữ nguyên bài để tiếp tục.",
                    }
                image_failure_reason = str(
                    generation.get("error") or "Không lấy được ảnh nguồn và không tạo được ảnh AI hợp lệ."
                )
                update_post_status(
                    generation["post_id"], "REJECTED", reject_reason=image_failure_reason
                )
                update_circle_article_match_status(circle_id, selected_article_id, "REJECTED")
                generation["error_code"] = "missing_valid_image"
                generation["error"] = (
                    f"Đã loại bài vì không có ảnh nguồn hợp lệ và ảnh AI không tạo được; "
                    f"đang tìm bài khác. {image_failure_reason}"
                )
                selection_attempts[-1]["reason"] = generation["error"]
            logger.info(
                f"{circle_name}: ứng viên #{selected_article_id} chưa đạt "
                f"(lần {_selection_attempt + 1}/3): {generation.get('error', '')}"
            )
            error_text = str(generation.get("error") or "").casefold()
            retryable_generation_error = generation.get("error_code") in {
                "article_not_suitable", "missing_valid_image", "validation_failed",
            } or any(marker in error_text for marker in (
                "đã được đăng trước đó",
                "chưa phù hợp để đăng",
                "nguồn bài gốc đã được đăng",
                "không tạo được ảnh hợp lệ",
            ))
            if not retryable_generation_error:
                break
    except Exception as exc:
        logger.error(f"Lỗi chu trình quét và tạo bài cho {circle_name}: {exc}")
        scan_result = {"success": False, "matched": 0, "error": str(exc)[:500]}
        generation = {"success": False, "error": str(exc)[:500]}
    success = bool(generation.get("success"))
    message = (
        f"Đã chọn tin nổi bật và tự tạo bài #{generation.get('post_id')} ở trạng thái Chờ đăng."
        if success
        else generation.get("error") or scan_result.get("error") or scan_result.get("message")
    )
    if not success and selection_attempts:
        message = f"Đã thẩm định {len(selection_attempts)} ứng viên. {message}"
        if not selected_article_id:
            last_reason = str(selection_attempts[-1].get("reason") or "")
            if last_reason:
                message += f" Lý do ứng viên gần nhất bị loại: {last_reason}"
    if not success and media_result.get("sources"):
        source_status_labels = {
            "disabled": "đã tắt",
            "missing_key": "thiếu API key",
            "missing_page_config": "chưa có Page được cấp quyền",
            "no_matching_authorized_page_posts": "Page được cấp quyền chưa có bài khớp chủ đề",
            "link_only": "chỉ được chia sẻ liên kết",
            "error": "nguồn đang lỗi",
        }
        source_report = ", ".join(
            f"{item.get('platform')}: tìm {item.get('found', 0)}, lưu mới {item.get('saved', 0)}"
            + (f", dùng lại {item.get('reused')} tin trong kho" if item.get("reused") else "")
            + (f", {item.get('queries')} truy vấn" if item.get("queries") else "")
            + (
                f", {source_status_labels.get(item.get('status'), item.get('status'))}"
                if item.get("status") != "ok" else ""
            )
            for item in media_result["sources"]
        )
        message = f"{message} Nguồn vừa quét: {source_report}."
    return {
        "success": success,
        "circle_id": circle_id,
        "circle_name": circle_name,
        "matched": int(scan_result.get("matched", 0) or 0),
        "generated": 1 if success else 0,
        "post_id": generation.get("post_id"),
        "message": message,
        "error": "" if success else message,
        "selection_mode": scan_result.get("selection_mode", "ai"),
        "warning": generation.get("warning") or scan_result.get("warning", ""),
        "scan": scan_result,
        "generation": generation,
        "media_collection": media_result,
        "selection_attempts": selection_attempts,
        "status": generation.get("status"),
    }


@app.post("/api/queue/clear")
async def clear_queue():
    """Xóa toàn bộ hàng đợi QUEUED để thu thập lại từ đầu."""
    conn = get_connection()
    try:
        result = conn.execute("DELETE FROM articles WHERE status = 'QUEUED'")
        count = result.rowcount
        conn.commit()
        logger.info(f"🗑️ Đã xóa {count} bài khỏi hàng đợi")
        return {"success": True, "cleared": count, "message": f"Đã dọn sạch {count} bài khỏi hàng đợi!"}
    finally:
        conn.close()


@app.post("/api/articles/{article_id}/generate")
async def generate_from_article(article_id: int, circle_id: Optional[str] = None):
    """Tạo bài viết AI từ 1 tin cụ thể trong hàng đợi."""
    logger.info(f"⚡ Đang chạy AI tạo bài từ tin #{article_id}...")
    result = await asyncio.to_thread(
        process_single_article, article_id, circle_id or "circle_tintuc24h"
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Lỗi tạo bài"))
    return result


@app.post("/api/queue/generate-best")
async def generate_from_best_queued(circle_id: Optional[str] = None):
    """Lấy tin tốt nhất trong hàng đợi và tạo bài viết AI."""
    from src.storage import get_best_queued_article
    best = get_best_queued_article()
    if not best:
        raise HTTPException(status_code=404, detail="Hàng đợi không còn bài nào hợp lệ")
    logger.info(f"⚡ Đang chạy AI tạo bài từ tin HOT nhất #{best['id']}: '{best.get('original_title')}'...")
    result = await asyncio.to_thread(
        process_single_article, best["id"], circle_id or "circle_tintuc24h"
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Lỗi tạo bài"))
    return result


@app.post("/api/ucircle/circles/{circle_id}/generate-best")
async def generate_best_for_circle(circle_id: str):
    """Dùng prompt riêng để chọn và tạo một bài cho đúng Circle."""
    from src.circle_selector import select_article_for_circle

    selection = await asyncio.to_thread(select_article_for_circle, circle_id)
    if not selection.get("success"):
        return JSONResponse(
            {"success": False, "error": selection.get("error"), "selection": selection},
            status_code=400,
        )
    result = await asyncio.to_thread(
        process_single_article, selection["article_id"], circle_id
    )
    if not result.get("success"):
        return JSONResponse(
            {"success": False, "error": result.get("error", "Lỗi tạo bài")},
            status_code=400,
        )
    result["selection"] = selection
    return result


@app.post("/api/ucircle/circles/{circle_id}/articles/{article_id}/generate")
async def generate_circle_article(circle_id: str, article_id: int):
    """Tạo bài AI từ một tin đã được phân loại vào đúng hàng chờ Circle."""
    if not claim_circle_article_match(circle_id, article_id):
        return JSONResponse(
            {"success": False, "error": "Tin này không còn trong hàng chờ của Circle hoặc đang được xử lý."},
            status_code=409,
        )
    result = await asyncio.to_thread(process_single_article, article_id, circle_id)
    if result.get("success"):
        update_circle_article_match_status(circle_id, article_id, "GENERATED")
        return result

    article_status = None
    conn = get_connection()
    try:
        row = conn.execute("SELECT status FROM articles WHERE id = ?", (article_id,)).fetchone()
        article_status = row[0] if row else None
    finally:
        conn.close()
    update_circle_article_match_status(
        circle_id,
        article_id,
        "SKIPPED" if article_status in {"REJECTED", "PROCESSED", "DUPLICATE"} else "MATCHED",
    )
    return JSONResponse(
        {"success": False, "error": result.get("error", "Lỗi tạo bài")},
        status_code=400,
    )


@app.get("/api/posts/{post_id}/overview")
async def get_post_overview(post_id: int):
    """Lấy thông tin tổng thể bài viết và đánh giá toàn diện điều kiện đăng."""
    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy bài viết #{post_id}")

    circle = get_circle_config(post.get("circle_id")) or {}
    post["circle_name"] = circle.get("circle_name") or post.get("circle_id")
    eval_result = evaluate_post_eligibility(post)

    return {
        "post": post,
        "evaluation": eval_result,
        "image": get_latest_image_asset(post_id),
        "image_current": current_image_is_approved(post, circle),
    }


@app.post("/api/posts/{post_id}/image")
async def continue_post_image(post_id: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Yêu cầu xử lý ảnh phải là một đối tượng JSON.")
    regenerate = data.get("regenerate", False)
    if not isinstance(regenerate, bool):
        raise HTTPException(status_code=422, detail="regenerate phải là true hoặc false.")
    result = await asyncio.to_thread(complete_post_image, post_id, force_regenerate=regenerate)
    code = {"not_found": 404, "forbidden": 403, "busy": 409}.get(result.get("error_code"), 200)
    return JSONResponse(result, status_code=code)


@app.post("/api/posts/{post_id}/image/manual-approve")
async def manual_approve_image(post_id: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict) or data.get("confirm") is not True:
        raise HTTPException(status_code=422, detail="Cần xác nhận đã xem ảnh và có quyền sử dụng ảnh.")
    result = await asyncio.to_thread(manually_approve_post_image, post_id)
    code = {
        "not_found": 404,
        "forbidden": 403,
        "busy": 409,
        "missing_image": 409,
        "source_only": 409,
        "missing_attribution": 409,
    }.get(result.get("error_code"), 200)
    return JSONResponse(result, status_code=code)


@app.post("/api/posts/{post_id}/status")
async def change_status(post_id: int, request: Request):
    data = await request.json()
    new_status = data.get("status")
    reason = data.get("reason")
    if new_status not in ["READY", "REVIEW", "REJECTED", "DRAFT"]:
        raise HTTPException(status_code=400, detail="Trạng thái không hợp lệ")

    post = get_post_by_id(post_id)
    if not post:
        raise HTTPException(status_code=404, detail=f"Không tìm thấy bài viết #{post_id}")
    if post.get("status") == "PUBLISHED":
        raise HTTPException(status_code=409, detail="Bài đã xuất bản nên không thể đổi lại trạng thái duyệt")
    if post.get("status") == "PUBLISHING" or post.get("image_work_state") == "PROCESSING":
        raise HTTPException(status_code=409, detail="Bài đang đăng hoặc xử lý ảnh; chờ thao tác hiện tại hoàn tất.")
    if new_status == "READY" and not current_image_is_approved(post, get_circle_config(post.get("circle_id")) or {}):
        raise HTTPException(status_code=409, detail="Ảnh chưa được xác minh với nội dung hiện tại; hãy bấm Kiểm tra / tiếp tục ảnh.")

    update_post_status(post_id, new_status, reject_reason=reason)
    logger.info(f"Updated Post #{post_id} -> {new_status}")
    return {"success": True, "post_id": post_id, "status": new_status}


_queue_publish_cancel = False


@app.post("/api/posts/publish-queue")
async def publish_queue(request: Request):
    """
    Chạy liên tục chu trình quét -> tạo -> đăng cho các Circle được bật.
    """
    global _queue_publish_cancel, _queue_publish_pause
    if _bg_status["queue_publish"]:
        return JSONResponse(
            {"status": "already_running",
             "message": "Đang chạy publish queue rồi...",
             "progress": _queue_publish_progress},
            status_code=409
        )

    data = await request.json()
    resume_requested = bool(data.get("resume"))
    resume_checkpoint = _load_publish_checkpoint() if resume_requested else {}
    try:
        requested_interval = float(data.get("interval_minutes", 30))
    except (TypeError, ValueError):
        return JSONResponse(
            {"status": "invalid", "message": "Khoảng nghỉ giữa các bài không hợp lệ."},
            status_code=422,
        )
    if requested_interval <= 0:
        return JSONResponse(
            {"status": "invalid", "message": "Khoảng nghỉ phải lớn hơn 0 phút."},
            status_code=422,
        )

    min_interval = 30
    automation_config = {}
    try:
        with open(ROOT_DIR / "config" / "automation.json", encoding="utf-8") as f:
            automation_config = json.load(f)
        min_interval = max(
            0,
            int(automation_config.get("publish_schedule", {}).get("min_interval_minutes", 30) or 0),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Không đọc được min_interval_minutes; dùng mặc định 30 phút.")

    interval_minutes = max(requested_interval, float(min_interval))
    interval_adjusted = interval_minutes != requested_interval
    interval_sec = max(5, int(interval_minutes * 60))
    time_display = f"{interval_sec} giây" if interval_sec < 60 else f"{round(interval_minutes, 1)} phút"

    discovered_circles = get_discovered_circles()
    enabled_circle_ids = [
        item.get("local_circle_id")
        for item in discovered_circles
        if item.get("publish_enabled") and item.get("scan_enabled") and item.get("local_circle_id")
    ]
    requested_circle_ids = data.get("circle_ids")
    if requested_circle_ids is not None:
        if not isinstance(requested_circle_ids, list):
            return JSONResponse(
                {"status": "invalid", "message": "Danh sách Circle được chọn không hợp lệ."},
                status_code=422,
            )
        requested_circle_ids = {str(item) for item in requested_circle_ids if item}
        enabled_circle_ids = [item for item in enabled_circle_ids if item in requested_circle_ids]
        if not enabled_circle_ids:
            return JSONResponse(
                {"status": "empty", "message": "Không có Circle được chọn nào đang bật cả Quét bài và Đăng bài."},
                status_code=200,
            )
    if not enabled_circle_ids:
        return JSONResponse({
            "status": "empty",
            "message": "Không có Circle nào đang bật cả Quét bài và Đăng bài."
        }, status_code=200)

    _queue_publish_cancel = False
    _queue_publish_pause = False
    _queue_publish_resume_event.set()
    _bg_status["queue_publish"] = True
    saved_successful = [
        circle_id for circle_id in resume_checkpoint.get("successful_circle_ids", [])
        if circle_id in enabled_circle_ids
    ]
    discovered_by_id = {
        str(item.get("local_circle_id")): item
        for item in discovered_circles if item.get("local_circle_id")
    }
    saved_circle_statuses = resume_checkpoint.get("circle_statuses", {})
    circle_statuses = {}
    init_now = datetime.now()
    for circle_id in enabled_circle_ids:
        circle = discovered_by_id.get(str(circle_id), {})
        saved_status = saved_circle_statuses.get(circle_id, {}) if isinstance(saved_circle_statuses, dict) else {}
        if not isinstance(saved_status, dict):
            saved_status = {}
        saved_state = str(saved_status.get("status") or "") if resume_requested else ""
        if saved_state == "stopped":
            saved_state = str(saved_status.get("resume_status") or "")
        if saved_state == "succeeded":
            saved_state = "resting"
        if saved_state == "failed":
            saved_state = "retry_wait"
        if saved_state in {"resting", "retry_wait", "pending", "ready_to_publish", "preparing", "publishing", "paused"}:
            state = saved_state
            message = str(saved_status.get("message") or "Tiếp tục từ checkpoint đã lưu.")
        else:
            state = "resting" if resume_requested and circle_id in saved_successful else "pending"
            message = (
                "Đã đăng thành công; tiếp tục theo lịch nghỉ riêng của Circle."
                if state == "resting" else "Đang chờ xử lý."
            )
        wait_until = str(saved_status.get("wait_until") or "") if resume_requested else ""
        if resume_requested and state in {"resting", "retry_wait"} and not wait_until:
            try:
                updated_at = datetime.fromisoformat(str(saved_status.get("updated_at") or ""))
            except (TypeError, ValueError):
                updated_at = init_now
            if state == "resting":
                wait_seconds = interval_sec
            else:
                saved_attempt = int(saved_status.get("attempt", resume_checkpoint.get("attempt", 1)) or 1)
                wait_seconds = min(300, 60 * (2 ** min(max(saved_attempt - 1, 0), 4)))
            wait_until = (updated_at + timedelta(seconds=wait_seconds)).isoformat(timespec="seconds")
        try:
            saved_cycle = int(saved_status.get("cycle", resume_checkpoint.get("cycle", 0)) or 0)
            saved_attempt = int(saved_status.get("attempt", resume_checkpoint.get("attempt", 0)) or 0)
        except (TypeError, ValueError):
            saved_cycle = saved_attempt = 0
        if state == "resting":
            saved_attempt = 0
        circle_statuses[circle_id] = {
            "circle_id": circle_id,
            "circle_name": circle.get("circle_name") or circle.get("display_name") or circle_id,
            "status": state,
            "message": message,
            "post_id": saved_status.get("post_id") if isinstance(saved_status, dict) else None,
            "updated_at": str(saved_status.get("updated_at") or init_now.isoformat(timespec="seconds")),
            "wait_until": wait_until,
            "cycle": saved_cycle,
            "attempt": saved_attempt,
        }

    resting_ids = [circle_id for circle_id, item in circle_statuses.items() if item["status"] == "resting"]
    active_pending_ids = [circle_id for circle_id, item in circle_statuses.items() if item["status"] != "resting"]

    now_text = datetime.now().isoformat(timespec="seconds")
    _queue_publish_progress["run_id"] = (
        str(resume_checkpoint.get("run_id") or resume_checkpoint.get("started_at") or now_text)
        if resume_requested else datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
    )
    _queue_publish_progress["started_at"] = (
        str(resume_checkpoint.get("started_at") or now_text) if resume_requested else now_text
    )
    _queue_publish_progress["ended_at"] = ""
    _queue_publish_progress["current"] = len(resting_ids)
    _queue_publish_progress["total"] = len(enabled_circle_ids)
    _queue_publish_progress["interval_sec"] = interval_sec
    _queue_publish_progress["cycle"] = max((item["cycle"] for item in circle_statuses.values()), default=0)
    _queue_publish_progress["stage"] = "starting"
    _queue_publish_progress["state"] = "running"
    _queue_publish_progress["completed"] = len(resting_ids)
    _queue_publish_progress["succeeded"] = len(resting_ids)
    _queue_publish_progress["failed"] = sum(item["status"] == "retry_wait" for item in circle_statuses.values())
    _queue_publish_progress["attempt"] = int(resume_checkpoint.get("attempt", 0) or 0) if resume_requested else 0
    _queue_publish_progress["pending"] = len(active_pending_ids)
    _queue_publish_progress["paused"] = False
    _queue_publish_progress["remaining_sec"] = 0
    _queue_publish_progress["mode"] = "independent_circle_v1"
    _queue_publish_progress["selected_circle_ids"] = list(enabled_circle_ids)
    _queue_publish_progress["pending_circle_ids"] = list(active_pending_ids)
    _queue_publish_progress["successful_circle_ids"] = list(resting_ids)
    _queue_publish_progress["circle_statuses"] = circle_statuses
    _queue_publish_progress["published_post_ids"] = (
        list(resume_checkpoint.get("published_post_ids", []))[-100:] if resume_requested else []
    )
    if resume_requested:
        _queue_publish_progress["log"] = list(resume_checkpoint.get("log", []))[-299:]
        _queue_publish_progress["log"].append(
            f"▶️ Tiếp tục checkpoint: giữ riêng lịch chạy của {len(enabled_circle_ids)} Circle; "
            f"{len(resting_ids)} đang nghỉ, {len(active_pending_ids)} đang cần xử lý."
        )
    else:
        _queue_publish_progress["log"] = [
            f"🆕 Chế độ độc lập cho {len(enabled_circle_ids)} Circle: "
            "mỗi Circle nghỉ riêng sau khi đăng thành công; Circle lỗi tự thử lại tối đa mỗi 5 phút."
        ]
    _save_publish_checkpoint()

    def _queue_publish_worker():
        import time as time_mod
        global _queue_publish_cancel, _queue_publish_pause
        from concurrent.futures import ThreadPoolExecutor, as_completed

        circle_status_lock = threading.Lock()

        def refresh_summary(statuses: dict):
            def resumed_status(value: dict) -> str:
                status = str(value.get("status") or "")
                return str(value.get("resume_status") or "") if status == "stopped" else status

            resting = [key for key, value in statuses.items() if resumed_status(value) == "resting"]
            pending = [key for key, value in statuses.items() if resumed_status(value) != "resting"]
            _queue_publish_progress["successful_circle_ids"] = resting
            _queue_publish_progress["pending_circle_ids"] = pending
            _queue_publish_progress["succeeded"] = len(resting)
            _queue_publish_progress["completed"] = len(resting)
            _queue_publish_progress["current"] = len(resting)
            _queue_publish_progress["pending"] = len(pending)
            _queue_publish_progress["failed"] = sum(resumed_status(value) == "retry_wait" for value in statuses.values())
            _queue_publish_progress["cycle"] = max(
                (int(value.get("cycle", 0) or 0) for value in statuses.values()), default=0
            )
            _queue_publish_progress["attempt"] = max(
                (int(value.get("attempt", 0) or 0) for value in statuses.values()), default=0
            )
            states = {resumed_status(value) for value in statuses.values()}
            if "publishing" in states:
                _queue_publish_progress["stage"] = "circle_publishing"
            elif "preparing" in states or "ready_to_publish" in states:
                _queue_publish_progress["stage"] = "circle_preparing"
            elif "retry_wait" in states:
                _queue_publish_progress["stage"] = "retry_wait"
            elif states and states <= {"resting"}:
                _queue_publish_progress["stage"] = "circle_resting"
            else:
                _queue_publish_progress["stage"] = "circles_running"

        def append_log(message: str):
            with circle_status_lock:
                _queue_publish_progress["log"].append(message)
                _queue_publish_progress["log"] = _queue_publish_progress["log"][-300:]
                _save_publish_checkpoint()
            logger.info(message)

        def set_circle_status(
            circle_id: str, status: str, message: str, post_id: int | None = None, **details
        ):
            circle = discovered_by_id.get(str(circle_id), {})
            with circle_status_lock:
                statuses = dict(_queue_publish_progress.get("circle_statuses") or {})
                previous = dict(statuses.get(circle_id, {}))
                entry = {
                    **previous,
                    "circle_id": circle_id,
                    "circle_name": circle.get("circle_name") or circle.get("display_name") or previous.get("circle_name") or circle_id,
                    "status": status,
                    "message": str(message or ""),
                    "post_id": post_id if post_id is not None else previous.get("post_id"),
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                if status not in {"resting", "retry_wait", "stopped"} and "wait_until" not in details:
                    entry["wait_until"] = ""
                entry.update(details)
                statuses[circle_id] = entry
                _queue_publish_progress["circle_statuses"] = statuses
                refresh_summary(statuses)
                _save_publish_checkpoint()

        def wait_if_paused(resume_stage: str) -> bool:
            if not _queue_publish_pause:
                return not _queue_publish_cancel
            if _queue_publish_progress.get("state") != "paused":
                _queue_publish_progress["resume_stage"] = resume_stage
                _queue_publish_progress["state"] = "paused"
                _queue_publish_progress["stage"] = "paused"
                _queue_publish_progress["paused"] = True
                append_log("⏸ Đã tạm dừng an toàn. Lịch riêng của từng Circle đã được lưu.")
            while _queue_publish_pause and not _queue_publish_cancel:
                _queue_publish_resume_event.wait(timeout=1)
            if _queue_publish_cancel:
                return False
            _queue_publish_progress["state"] = "running"
            _queue_publish_progress["stage"] = resume_stage
            _queue_publish_progress["paused"] = False
            _save_publish_checkpoint()
            return True

        def wait_for_circle(circle_id: str, seconds: int, status: str, message: str,
                            cycle: int, attempt: int, post_id: int | None = None) -> bool:
            remaining = max(0, int(seconds))
            wait_until = (datetime.now() + timedelta(seconds=remaining)).isoformat(timespec="seconds")
            set_circle_status(
                circle_id, status, message, post_id,
                wait_until=wait_until, remaining_sec=remaining, cycle=cycle, attempt=attempt,
            )
            last_checkpoint = remaining
            while remaining > 0:
                if not wait_if_paused("circle_wait"):
                    set_circle_status(
                        circle_id, "stopped", "Đã dừng; lịch chờ riêng được lưu để tiếp tục.", post_id,
                        resume_status=status, wait_until=wait_until,
                        remaining_sec=remaining, cycle=cycle, attempt=attempt,
                    )
                    return False
                time_mod.sleep(1)
                remaining -= 1
                if remaining == 0 or last_checkpoint - remaining >= 15:
                    wait_until = (datetime.now() + timedelta(seconds=remaining)).isoformat(timespec="seconds")
                    set_circle_status(
                        circle_id, status, message, post_id,
                        wait_until=wait_until, remaining_sec=remaining, cycle=cycle, attempt=attempt,
                    )
                    last_checkpoint = remaining
            return not _queue_publish_cancel

        def prepare_circle(circle_id: str) -> dict:
            if not wait_if_paused("circle_preparing"):
                return {"success": False, "circle_id": circle_id, "error": "Đã dừng trước khi chuẩn bị Circle."}
            set_circle_status(circle_id, "preparing", "Đang quét nguồn, chọn tin và tạo/tiếp tục bài.")
            circle = get_circle_config(circle_id) or {}
            circle_name = circle.get("display_name") or circle.get("circle_name") or circle_id
            ready_posts = get_posts_by_status("READY", circle_id=circle_id)
            post = ready_posts[-1] if ready_posts else None
            if post:
                set_circle_status(circle_id, "ready_to_publish", "Đã có bài hợp lệ, chuẩn bị đăng.", int(post["id"]))
                return {"success": True, "circle_id": circle_id, "circle_name": circle_name,
                        "post_id": int(post["id"]), "from_ready": True}
            workflow = _scan_and_generate_for_circle(
                circle_id, control=lambda: wait_if_paused("circle_preparing")
            )
            if workflow.get("success") and workflow.get("post_id"):
                set_circle_status(circle_id, "ready_to_publish", "Đã chuẩn bị xong bài, chờ đăng.", int(workflow["post_id"]))
            return {
                "success": bool(workflow.get("success")), "circle_id": circle_id,
                "circle_name": circle_name, "post_id": workflow.get("post_id"),
                "from_ready": False, "error": workflow.get("error", ""),
            }

        def run_one_circle(circle_id: str):
            saved = (_queue_publish_progress.get("circle_statuses") or {}).get(circle_id, {})
            cycle = int(saved.get("cycle", 0) or 0)
            attempt = int(saved.get("attempt", 0) or 0)
            first_status = str(saved.get("status") or "pending")
            if first_status == "stopped":
                first_status = str(saved.get("resume_status") or "pending")
            if first_status in {"retry_wait", "pending", "preparing", "ready_to_publish", "publishing"} and attempt:
                # Checkpoint ghi cycle đang thử; biến cục bộ cần giữ cycle thành công gần nhất.
                cycle = max(0, cycle - 1)
            if first_status in {"resting", "retry_wait"}:
                wait_until = str(saved.get("wait_until") or "")
                try:
                    due = datetime.fromisoformat(wait_until)
                    initial_wait = max(0, int((due - datetime.now()).total_seconds()))
                except (TypeError, ValueError):
                    initial_wait = interval_sec if first_status == "resting" else 60
                if initial_wait and not wait_for_circle(
                    circle_id, initial_wait, first_status,
                    "Đang giữ lịch nghỉ riêng của Circle." if first_status == "resting" else "Đang chờ thử lại riêng cho Circle.",
                    cycle, attempt, saved.get("post_id"),
                ):
                    return

            while not _queue_publish_cancel:
                if not wait_if_paused("circle_preparing"):
                    break
                target_cycle = cycle + 1
                attempt += 1
                set_circle_status(
                    circle_id, "preparing", f"Đang chạy lượt riêng #{target_cycle}, lần thử #{attempt}.",
                    cycle=target_cycle, attempt=attempt,
                )
                circle = get_circle_config(circle_id) or {}
                circle_name = circle.get("display_name") or circle.get("circle_name") or circle_id
                post_id = None
                error = ""
                try:
                    prepared = prepare_circle(circle_id)
                    post_id = int(prepared["post_id"]) if prepared.get("post_id") else None
                    if not prepared.get("success") or not post_id:
                        error = prepared.get("error") or "Chưa chuẩn bị được bài."
                    elif not wait_if_paused("circle_publishing"):
                        error = "Đã dừng trước khi đăng bài."
                    else:
                        post = get_post_by_id(post_id)
                        if not post or post.get("status") not in {"READY", "PUBLISHED"}:
                            error = f"Bài #{post_id} chưa ở trạng thái có thể đăng; sẽ chuẩn bị lại."
                        else:
                            set_circle_status(circle_id, "publishing", "Đang đăng và xác minh trên Circle.", post_id,
                                              cycle=target_cycle, attempt=attempt)
                            published = publish_post(
                                post_id,
                                max_retries=max(1, int(automation_config.get("publish_schedule", {}).get("max_publish_retries", 3))),
                                manual_override=True,
                            )
                            if not published:
                                error = get_last_publish_error(post_id) or "Đăng bài chưa thành công."
                except Exception as exc:
                    error = str(exc)[:300]

                if not error and post_id:
                    cycle = target_cycle
                    attempt = 0
                    with circle_status_lock:
                        _queue_publish_progress["published_post_ids"].append(post_id)
                        _queue_publish_progress["published_post_ids"] = _queue_publish_progress["published_post_ids"][-100:]
                    append_log(f"✅ {circle_name} · bài #{post_id}: đăng và xác minh thành công; nghỉ riêng {time_display}.")
                    due = datetime.now() + timedelta(seconds=interval_sec)
                    if not wait_for_circle(
                        circle_id, interval_sec, "resting",
                        f"Đăng thành công; Circle này nghỉ đến {due.strftime('%H:%M:%S')} rồi tự chạy lượt kế tiếp.",
                        cycle, attempt, post_id,
                    ):
                        break
                else:
                    retry_delay = min(300, 60 * (2 ** min(max(attempt - 1, 0), 4)))
                    append_log(
                        f"⚠️ {circle_name}: {error or 'chưa đăng thành công'}; "
                        f"Circle này sẽ tự thử lại sau tối đa 5 phút (lần này {round(retry_delay / 60, 1)} phút)."
                    )
                    if not wait_for_circle(
                        circle_id, retry_delay, "retry_wait",
                        f"Lỗi: {error or 'chưa đăng thành công'}. Circle này tự thử lại sau {round(retry_delay / 60, 1)} phút.",
                        target_cycle, attempt, post_id,
                    ):
                        break

            status_entry = (_queue_publish_progress.get("circle_statuses") or {}).get(circle_id, {})
            if _queue_publish_cancel and status_entry.get("status") != "stopped":
                prior = status_entry.get("status") or "pending"
                resume_status = prior if prior in {"resting", "retry_wait", "ready_to_publish"} else "pending"
                set_circle_status(
                    circle_id, "stopped", "Đã dừng; tiến trình riêng của Circle đã lưu.",
                    status_entry.get("post_id"), resume_status=resume_status,
                    wait_until=status_entry.get("wait_until", ""),
                    remaining_sec=status_entry.get("remaining_sec", 0),
                    cycle=cycle, attempt=attempt,
                )

        def supervise_circle(circle_id: str):
            while not _queue_publish_cancel:
                try:
                    run_one_circle(circle_id)
                    return
                except Exception as exc:
                    current = (_queue_publish_progress.get("circle_statuses") or {}).get(circle_id, {})
                    cycle = int(current.get("cycle", 0) or 0)
                    attempt = int(current.get("attempt", 0) or 0) + 1
                    retry_delay = min(300, 60 * (2 ** min(max(attempt - 1, 0), 4)))
                    name = current.get("circle_name") or circle_id
                    error = str(exc)[:250]
                    append_log(
                        f"⚠️ {name}: tác vụ bị lỗi ({error}); Circle này sẽ tự khởi động lại "
                        f"sau {round(retry_delay / 60, 1)} phút."
                    )
                    if not wait_for_circle(
                        circle_id, retry_delay, "retry_wait",
                        f"Lỗi tác vụ: {error}. Thử lại riêng sau {round(retry_delay / 60, 1)} phút.",
                        cycle, attempt, current.get("post_id"),
                    ):
                        return

        try:
            _queue_publish_progress["stage"] = "circles_running"
            append_log(
                f"🔄 Khởi chạy độc lập {len(enabled_circle_ids)} Circle: mỗi Circle tự quét, tạo, đăng "
                f"rồi nghỉ riêng {time_display}; Circle lỗi tự thử lại, không chặn các Circle khác."
            )
            with ThreadPoolExecutor(max_workers=min(6, len(enabled_circle_ids)), thread_name_prefix="circle-cycle") as executor:
                futures = {executor.submit(supervise_circle, circle_id): circle_id for circle_id in enabled_circle_ids}
                for future in as_completed(futures):
                    circle_id = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        circle = get_circle_config(circle_id) or {}
                        name = circle.get("display_name") or circle.get("circle_name") or circle_id
                        set_circle_status(circle_id, "retry_wait", f"Lỗi tác vụ Circle: {str(exc)[:250]}")
                        logger.exception(f"Tác vụ tự động riêng cho {name} gặp lỗi.")
        finally:
            if _queue_publish_cancel:
                append_log("⏹ Đã dừng hẳn; trạng thái và lịch chạy riêng của từng Circle được lưu để tiếp tục.")
                _queue_publish_progress["state"] = "stopped"
                _queue_publish_progress["stage"] = "stopped"
            else:
                _queue_publish_progress["state"] = "finished"
                _queue_publish_progress["stage"] = "idle"
            _queue_publish_progress["paused"] = False
            _queue_publish_progress["ended_at"] = datetime.now().isoformat(timespec="seconds")
            _bg_status["queue_publish"] = False
            _save_publish_checkpoint()
            _archive_publish_progress(_queue_publish_progress)

    threading.Thread(target=_queue_publish_worker, daemon=True).start()
    adjustment_note = (
        f" Khoảng nghỉ đã được tự động nâng lên mức an toàn {min_interval} phút."
        if interval_adjusted else ""
    )
    return {
        "status": "started",
        "message": (
            f"Đã bật lịch tự động độc lập cho {len(enabled_circle_ids)} Circle; mỗi Circle tự nghỉ "
            f"{time_display} sau khi đăng thành công, Circle lỗi tự thử lại tối đa mỗi 5 phút.{adjustment_note}"
        ),
        "total": len(enabled_circle_ids),
        "interval_sec": interval_sec,
        "interval_display": time_display,
        "interval_adjusted": interval_adjusted,
        "min_interval_minutes": min_interval,
    }


@app.post("/api/posts/publish-queue/stop")
async def stop_publish_queue():
    """Dừng hẳn worker tại điểm an toàn và giữ checkpoint để tiếp tục sau."""
    global _queue_publish_cancel, _queue_publish_pause
    if not _bg_status["queue_publish"]:
        _queue_publish_progress["state"] = "stopped"
        _queue_publish_progress["stage"] = "stopped"
        _queue_publish_progress["paused"] = False
        _queue_publish_progress["ended_at"] = datetime.now().isoformat(timespec="seconds")
        _save_publish_checkpoint()
        _archive_publish_progress(_queue_publish_progress)
        return {
            "status": "stopped",
            "message": "Chu trình đã dừng; checkpoint vẫn được giữ để Tiếp tục.",
        }
    current_stage = _queue_publish_progress.get("stage", "")
    if current_stage not in {"pausing", "paused", "stopping", "stopped"}:
        _queue_publish_progress["resume_stage"] = current_stage
    _queue_publish_cancel = True
    _queue_publish_pause = False
    _queue_publish_resume_event.set()
    _queue_publish_progress["state"] = "stopping"
    _queue_publish_progress["stage"] = "stopping"
    _queue_publish_progress["paused"] = False
    if not any("Đang dừng hẳn" in line for line in _queue_publish_progress.get("log", [])[-3:]):
        _queue_publish_progress.setdefault("log", []).append(
            "⏹ Đang dừng hẳn sau thao tác hiện tại; checkpoint đã được lưu."
        )
    _save_publish_checkpoint()
    return {
        "status": "stopping",
        "message": "Đã nhận lệnh. Hệ thống đang dừng an toàn sau thao tác hiện tại.",
    }


@app.post("/api/posts/publish-queue/pause")
async def pause_publish_queue():
    """Tạm dừng worker nhưng giữ thread và toàn bộ tiến trình hiện tại."""
    global _queue_publish_pause
    if not _bg_status["queue_publish"]:
        return JSONResponse(
            {"status": "not_running", "message": "Chu trình chưa chạy; hãy dùng Tiếp tục checkpoint."},
            status_code=409,
        )
    if _queue_publish_pause:
        return {"status": "paused", "message": "Chu trình đang tạm dừng."}
    _queue_publish_progress["resume_stage"] = _queue_publish_progress.get("stage", "circle_preparing")
    _queue_publish_pause = True
    _queue_publish_resume_event.clear()
    _queue_publish_progress["state"] = "pausing"
    _queue_publish_progress["stage"] = "pausing"
    _queue_publish_progress.setdefault("log", []).append(
        "⏸ Đã nhận lệnh tạm dừng; đang chờ điểm an toàn của thao tác hiện tại."
    )
    _save_publish_checkpoint()
    return {
        "status": "pausing",
        "message": "Đã nhận lệnh tạm dừng; tiến trình và checkpoint được giữ nguyên.",
    }


@app.post("/api/posts/publish-queue/resume")
async def resume_publish_queue():
    """Tiếp tục thread đang tạm dừng hoặc khởi chạy lại từ checkpoint đã dừng."""
    global _queue_publish_pause, _queue_publish_cancel
    if _bg_status["queue_publish"]:
        if not _queue_publish_pause and _queue_publish_progress.get("state") not in {"paused", "pausing"}:
            return {"status": "running", "message": "Chu trình đang chạy."}
        _queue_publish_pause = False
        _queue_publish_cancel = False
        _queue_publish_resume_event.set()
        _queue_publish_progress["state"] = "running"
        _queue_publish_progress["stage"] = _queue_publish_progress.get("resume_stage") or "circle_preparing"
        _queue_publish_progress["paused"] = False
        _queue_publish_progress.setdefault("log", []).append("▶️ Đã tiếp tục tiến trình đang tạm dừng.")
        _save_publish_checkpoint()
        return {"status": "resumed", "message": "Đã tiếp tục tiến trình đang tạm dừng."}

    checkpoint = _load_publish_checkpoint()
    circle_ids = checkpoint.get("selected_circle_ids") or []
    if not circle_ids:
        return JSONResponse(
            {"status": "no_checkpoint", "message": "Chưa có checkpoint nào để tiếp tục."},
            status_code=404,
        )
    known_circle_ids = {
        str(item.get("local_circle_id"))
        for item in get_discovered_circles() if item.get("local_circle_id")
    }
    unknown_circle_ids = [str(circle_id) for circle_id in circle_ids if str(circle_id) not in known_circle_ids]
    if unknown_circle_ids:
        return JSONResponse(
            {
                "status": "invalid_checkpoint",
                "message": (
                    "Checkpoint đã lưu có Circle không còn trong danh sách tài khoản; "
                    "không tiếp tục để tránh chạy nhầm. Hãy bắt đầu một lượt tự động mới. "
                    "Checkpoint cũ vẫn được giữ nguyên."
                ),
            },
            status_code=409,
        )

    class _ResumeRequest:
        async def json(self):
            return {
                "interval_minutes": max(0.1, float(checkpoint.get("interval_sec", 1800) or 1800) / 60),
                "circle_ids": circle_ids,
                "resume": True,
            }

    return await publish_queue(_ResumeRequest())


@app.get("/api/posts/publish-queue/status")
async def get_queue_publish_status():
    """Kiểm tra tiến trình publish queue."""
    known_circle_ids = {
        str(item.get("local_circle_id"))
        for item in get_discovered_circles() if item.get("local_circle_id")
    }
    selected_circle_ids = _queue_publish_progress.get("selected_circle_ids") or []
    unknown_circle_ids = [str(circle_id) for circle_id in selected_circle_ids if str(circle_id) not in known_circle_ids]
    checkpoint_warning = None
    if unknown_circle_ids:
        checkpoint_warning = (
            "Checkpoint cũ không khớp Circle trong tài khoản hiện tại. Không bấm Tiếp tục; "
            "hãy bắt đầu lượt tự động mới. Log/checkpoint cũ chưa bị xóa."
        )
    return {
        "running": _bg_status["queue_publish"],
        "progress": _queue_publish_progress,
        "checkpoint_warning": checkpoint_warning,
    }


@app.get("/api/posts/publish-queue/history")
async def get_queue_publish_history(limit: int = 20):
    history = _load_publish_history()
    safe_limit = max(1, min(int(limit), 50))
    return {"success": True, "history": history[:safe_limit], "total": len(history)}


@app.delete("/api/posts/publish-queue/history")
async def delete_queue_publish_history(run_id: Optional[str] = None):
    """Xóa một phiên hoặc toàn bộ lịch sử; không đụng đến bài viết/checkpoint hiện tại."""
    with _publish_history_lock:
        history = _load_publish_history()
        if run_id:
            kept = [item for item in history if str(item.get("run_id")) != str(run_id)]
        else:
            kept = []
        removed = len(history) - len(kept)
        PUBLISH_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = PUBLISH_HISTORY_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(kept, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(PUBLISH_HISTORY_PATH)
    return {
        "success": True,
        "removed": removed,
        "message": f"Đã xóa {removed} bản ghi lịch sử; bài viết và checkpoint không thay đổi.",
    }


@app.post("/api/posts/{post_id}/publish")
async def publish_now(post_id: int):
    """Xuất bản bài viết lên UCircle an toàn từ Thread Pool (tránh lỗi Event Loop)."""
    logger.info(f"🚀 Bắt đầu đăng bài #{post_id} lên UCircle...")
    try:
        # Người dùng đã xác nhận "Đăng ngay", nên chỉ bỏ qua khoảng nghỉ
        # giữa hai bài. Giới hạn an toàn theo giờ/ngày vẫn được giữ nguyên.
        success = await asyncio.to_thread(publish_post, post_id, 1, True)
        if success:
            logger.info(f"✅ Đăng bài #{post_id} thành công!")
            return {
                "success": True,
                "post_id": post_id,
                "message": "Đã xác minh bài xuất hiện trên UCircle và xóa khỏi danh sách chờ.",
            }
        else:
            error_message = get_last_publish_error(post_id)
            logger.warning(f"❌ Không thể đăng bài #{post_id}: {error_message}")
            status_code = 400
            if "không tìm thấy" in error_message.lower():
                status_code = 404
            elif "trạng thái" in error_message.lower() or "tiến trình khác" in error_message.lower():
                status_code = 409
            elif "giới hạn" in error_message.lower() or "chờ thêm" in error_message.lower():
                status_code = 429
            return JSONResponse({
                "success": False,
                "post_id": post_id,
                "error": error_message
            }, status_code=status_code)
    except Exception as e:
        logger.error(f"❌ Lỗi ngoại lệ khi đăng bài #{post_id}: {e}", exc_info=True)
        return JSONResponse({
            "success": False,
            "post_id": post_id,
            "error": f"Lỗi hệ thống: {str(e)}"
        }, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("review_ui.app:app", host="127.0.0.1", port=8000, reload=True)
