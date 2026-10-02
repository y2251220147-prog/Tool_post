"""
src/queue_manager.py
Publish Queue Manager
Quản lý hàng đợi xuất bản, kiểm soát tần suất (rate limits) để tránh spam
"""

import json
from pathlib import Path
from datetime import datetime

from .storage import (
    get_posts_by_status,
    count_published_today,
    count_published_this_hour,
    get_last_successful_publish_at,
    update_post_status
)
from .logger import get_logger

logger = get_logger("queue_manager")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "automation.json"


def _get_publish_limits() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    return config.get("publish_schedule", {
        "max_posts_per_hour": 4,
        "max_posts_per_day": 20,
        "min_interval_minutes": 10
    })


def can_publish_now(circle_id: str, enforce_min_interval: bool = True) -> tuple[bool, str]:
    """
    Kiểm tra xem circle_id hiện tại có được phép đăng thêm bài hay không.

    Nút "Đăng ngay" có thể bỏ qua khoảng nghỉ tối thiểu do người dùng
    đã xác nhận thủ công. Giới hạn theo giờ/ngày luôn được giữ lại.
    """
    limits = _get_publish_limits()
    max_hour = limits.get("max_posts_per_hour", 4)
    max_day = limits.get("max_posts_per_day", 20)

    count_today = count_published_today(circle_id)
    if max_day is not None and int(max_day) > 0 and count_today >= int(max_day):
        return False, f"Đã đạt giới hạn số bài trong ngày ({count_today}/{max_day})"

    count_hour = count_published_this_hour(circle_id)
    if count_hour >= max_hour:
        return False, f"Đã đạt giới hạn số bài trong giờ ({count_hour}/{max_hour})"

    min_interval = int(limits.get("min_interval_minutes", 0) or 0)
    last_published_at = get_last_successful_publish_at(circle_id)
    if enforce_min_interval and min_interval > 0 and last_published_at:
        try:
            last_dt = datetime.strptime(last_published_at, "%Y-%m-%d %H:%M:%S")
            elapsed_minutes = (datetime.now() - last_dt).total_seconds() / 60
            if elapsed_minutes < min_interval:
                remaining = max(1, int(min_interval - elapsed_minutes + 0.999))
                return False, f"Cần chờ thêm khoảng {remaining} phút trước bài tiếp theo"
        except ValueError:
            pass

    return True, "OK"


def get_next_ready_post(circle_id: str = None) -> dict | None:
    """
    Lấy bài viết ở trạng thái READY sẵn sàng để đăng.
    """
    ready_posts = get_posts_by_status("READY", circle_id=circle_id)
    if not ready_posts:
        return None
    # Lấy bài cũ nhất trước (FIFO)
    return ready_posts[-1]
