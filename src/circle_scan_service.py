"""Điều phối thu thập và phân loại nhiều Circle song song có giới hạn."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from time import monotonic

from .circle_selector import scan_articles_for_circle
from .media_collector import collect_media_for_circle
from .ucircle_account import get_circle_config


def _scan_one_circle(circle_id: str) -> dict:
    circle = get_circle_config(circle_id)
    circle_name = circle.get("display_name") or circle.get("circle_name") or circle_id
    if not circle:
        return {"circle_id": circle_id, "circle_name": circle_id, "success": False, "error": "Không tìm thấy cấu hình Circle."}
    if not circle.get("ownership_verified"):
        return {"circle_id": circle_id, "circle_name": circle_name, "success": False, "error": "Circle chưa được xác minh quyền sở hữu."}
    if not circle.get("scan_enabled", False):
        return {"circle_id": circle_id, "circle_name": circle_name, "success": False, "error": "Circle chưa bật Quét bài."}

    started_at = monotonic()
    try:
        media = collect_media_for_circle(circle_id)
        scan = scan_articles_for_circle(circle_id)
        return {
            "circle_id": circle_id,
            "circle_name": circle_name,
            "success": bool(scan.get("success")),
            "matched": int(scan.get("matched", 0) or 0),
            "media_saved": int(media.get("saved", 0) or 0),
            "media_sources": media.get("sources", []),
            "message": scan.get("message", ""),
            "error": scan.get("error", ""),
            "selection_mode": scan.get("selection_mode", "ai"),
            "warning": scan.get("warning", ""),
            "duration_seconds": round(monotonic() - started_at, 2),
        }
    except Exception as exc:
        return {
            "circle_id": circle_id,
            "circle_name": circle_name,
            "success": False,
            "matched": 0,
            "media_saved": 0,
            "media_sources": [],
            "error": str(exc)[:500],
            "duration_seconds": round(monotonic() - started_at, 2),
        }


def scan_circles_parallel(circle_ids: list[str], max_workers: int = 3) -> dict:
    """Quét nhiều Circle đồng thời; lỗi một Circle không dừng các Circle còn lại."""
    unique_ids = list(dict.fromkeys(str(item).strip() for item in circle_ids if str(item).strip()))
    if not unique_ids:
        return {
            "success": False,
            "message": "Chưa chọn Circle nào để quét.",
            "results": [],
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "matched": 0,
            "media_saved": 0,
        }

    started_at = monotonic()
    worker_count = max(1, min(int(max_workers), 3, len(unique_ids)))
    by_circle = {}
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="circle-scan") as executor:
        futures = {executor.submit(_scan_one_circle, circle_id): circle_id for circle_id in unique_ids}
        for future in as_completed(futures):
            circle_id = futures[future]
            try:
                by_circle[circle_id] = future.result()
            except Exception as exc:
                by_circle[circle_id] = {
                    "circle_id": circle_id,
                    "circle_name": circle_id,
                    "success": False,
                    "matched": 0,
                    "media_saved": 0,
                    "media_sources": [],
                    "error": str(exc)[:500],
                }

    results = [by_circle[circle_id] for circle_id in unique_ids]
    succeeded = sum(1 for item in results if item.get("success"))
    matched = sum(int(item.get("matched", 0) or 0) for item in results)
    media_saved = sum(int(item.get("media_saved", 0) or 0) for item in results)
    failed = len(results) - succeeded
    return {
        "success": failed == 0,
        "message": f"Đã quét xong {succeeded}/{len(results)} Circle; thu thập {media_saved} nguồn media mới và phân loại {matched} tin phù hợp.",
        "results": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "matched": matched,
        "media_saved": media_saved,
        "max_workers": worker_count,
        "duration_seconds": round(monotonic() - started_at, 2),
    }
