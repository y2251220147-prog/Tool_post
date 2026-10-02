"""Hoàn tất riêng phần ảnh của bài đã viết, không quét nguồn hay gọi Writer lại."""

from pathlib import Path

from .image_context import image_analysis
from .image_context import image_context_hash
from .image_generator import generate_and_save_image, replace_image_label
from .logger import get_logger
from .storage import (
    claim_post_for_image, get_post_by_id, release_post_image, get_connection,
    update_post_full_content, update_post_status, get_latest_image_asset, save_media_asset,
)
from .ucircle_account import get_circle_config
from .validator import validate_post

logger = get_logger("image_workflow")


def manually_approve_post_image(post_id: int) -> dict:
    """Lưu xác nhận của người dùng khi đã xem ảnh nhưng bộ kiểm tra tự động lỗi."""
    post = get_post_by_id(post_id)
    if not post:
        return {"success": False, "error_code": "not_found", "error": "Không tìm thấy bài viết."}
    if post.get("status") in {"PUBLISHED", "PUBLISHING"}:
        return {"success": False, "error_code": "busy", "error": "Bài đã đăng hoặc đang đăng."}
    if post.get("image_work_state") == "PROCESSING":
        return {
            "success": False,
            "error_code": "busy",
            "error": "Ảnh vẫn đang được kiểm tra. Hãy đợi thao tác đó kết thúc rồi mở lại bài để xác nhận thủ công.",
        }
    if not claim_post_for_image(post_id):
        return {
            "success": False,
            "error_code": "busy",
            "error": "Ảnh vừa được một tác vụ khác nhận xử lý. Hãy đợi tác vụ đó kết thúc rồi thử lại.",
        }

    try:
        post = get_post_by_id(post_id) or post
        circle = get_circle_config(post.get("circle_id")) or {}
        if not circle.get("ownership_verified"):
            return {"success": False, "error_code": "forbidden", "error": "Circle chưa được xác minh quyền sở hữu."}

        asset = get_latest_image_asset(post_id)
        file_path = str((asset or {}).get("file_path") or "")
        if not asset or not file_path or not Path(file_path).is_file():
            return {"success": False, "error_code": "missing_image", "error": "Chưa có file ảnh hợp lệ để xác nhận."}
        if circle.get("image_strategy") == "source_only" and bool(asset.get("is_ai_generated")):
            return {
                "success": False,
                "error_code": "source_only",
                "error": "Circle này chỉ cho phép ảnh thật từ nguồn; không thể xác nhận ảnh AI.",
            }
        if not bool(asset.get("is_ai_generated")) and not (asset.get("source_url") or asset.get("source_name")):
            return {
                "success": False,
                "error_code": "missing_attribution",
                "error": "Ảnh nguồn chưa có thông tin xuất xứ để ghi nhận.",
            }

        circle_name = circle.get("display_name") or circle.get("circle_name") or post.get("circle_id", "")
        label = (
            "🖼 Ảnh minh họa AI"
            if bool(asset.get("is_ai_generated"))
            else f"🖼 Ảnh từ bài gốc — Nguồn: {asset.get('source_name') or post.get('source_name') or 'nguồn bài viết'}"
        )
        post["full_content"] = replace_image_label(post.get("full_content", ""), label)
        update_post_full_content(post_id, post["full_content"])
        asset.update({
            "post_id": post_id,
            "type": "image",
            "context_hash": image_context_hash(post, image_analysis(post), circle),
            "validation_status": "PASS",
            "validation_reason": "Người dùng đã xem và xác nhận ảnh phù hợp thủ công; Gemini chưa xác minh ảnh này.",
            "relevance_score": 100,
            "validation_result": {
                "method": "manual_user_review",
                "approved_by_user": True,
                "note": "Người dùng xác nhận ảnh đúng nội dung và có quyền sử dụng; không phải kết quả Gemini.",
            },
        })
        asset.pop("id", None)
        save_media_asset(asset)

        image_meta = {
            "file_path": file_path,
            "status": "PASS",
            "is_ai_generated": bool(asset.get("is_ai_generated")),
            "image_label": label,
        }
        validation = validate_post(post, post, image_meta=image_meta)
        state = validation.get("status") or "REVIEW"
        update_post_status(post_id, state, reject_reason=None if state == "READY" else "; ".join(validation.get("reasons") or []))
        return {
            "success": True,
            "post_ready": state == "READY",
            "status": state,
            "post_id": post_id,
            "circle_name": circle_name,
            "message": (
                "Ảnh đã được xác nhận thủ công; bài đủ điều kiện và chuyển sang Chờ đăng."
                if state == "READY"
                else f"Ảnh đã được xác nhận thủ công, nhưng bài chưa đạt các điều kiện nội dung ({state})."
            ),
            "reasons": validation.get("reasons") or [],
        }
    finally:
        release_post_image(post_id)


def complete_post_image(post_id: int, *, force_regenerate: bool = False, control=None,
                        force_source: bool = False) -> dict:
    post = get_post_by_id(post_id)
    if not post:
        return {"success": False, "error": "Không tìm thấy bài viết.", "error_code": "not_found"}
    circle = get_circle_config(post.get("circle_id")) or {}
    if not circle.get("ownership_verified"):
        return {"success": False, "error": "Circle chưa được xác minh quyền sở hữu.", "error_code": "forbidden"}
    if post.get("status") in {"PUBLISHED", "PUBLISHING"}:
        return {"success": False, "error": "Bài đã đăng hoặc đang đăng; không thể thay ảnh.", "error_code": "busy"}
    if not claim_post_for_image(post_id):
        return {"success": False, "error": "Phần ảnh của bài đang được xử lý.", "error_code": "busy"}
    result = {"post_id": post_id, "circle_id": post.get("circle_id"), "title": post.get("ai_title")}
    try:
        conn = get_connection()
        try:
            source = conn.execute("SELECT source_image_url, thumbnail_url FROM articles WHERE id = ?",
                                  (post.get("article_id"),)).fetchone()
            if source:
                post.update(dict(source))
        finally:
            conn.close()
        image = generate_and_save_image(post_id, post, image_analysis(post), circle=circle,
                                        force_regenerate=force_regenerate, control=control, force_source=force_source)
        post["full_content"] = replace_image_label(post.get("full_content", ""), image["image_label"])
        update_post_full_content(post_id, post["full_content"])
        validation = validate_post(post, post, image_meta=image)
        if image.get("status") != "PASS":
            state = "WAITING_IMAGE"
            reason = image.get("reason") or "Ảnh chưa được xác nhận phù hợp nội dung."
            result.update({"success": False, "error_code": "image_pending", "error": reason})
        else:
            state = "READY" if validation["status"] == "READY" else "REJECTED"
            reason = "" if state == "READY" else "; ".join(validation.get("reasons") or [])
            result.update({"success": state == "READY", "error": reason,
                           "error_code": "" if state == "READY" else "validation_failed"})
        update_post_status(post_id, state, reject_reason=reason or None)
        result.update({"status": state, "image": image, "score": validation["score"]})
        return result
    except Exception as exc:
        logger.warning(f"Giữ bài #{post_id} Chờ ảnh sau lỗi {type(exc).__name__}")
        reason = "Phần ảnh chưa hoàn tất; bài đã viết được giữ nguyên để tiếp tục."
        update_post_status(post_id, "WAITING_IMAGE", reject_reason=reason)
        return {**result, "success": False, "status": "WAITING_IMAGE", "error_code": "image_pending", "error": reason}
    finally:
        release_post_image(post_id)
