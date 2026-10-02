"""
scheduler.py
Bo lap lich tu dong voi 2 job rieng biet:

  JOB 1 — Collection (moi 20 phut):
    RSS → Loc → Chong trung → Luu hang doi
    KHONG goi AI

  JOB 2 — Publish (moi 45 phut, trong khung 6h-23h):
    Chon bai tot nhat → Gemini → Kiem duyet → Dang UCircle → Log

Cach chay:
  python scheduler.py
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from collection_pipeline import run_collection
from publish_pipeline import run_publish
from src.logger import get_logger

logger = get_logger("scheduler")


def _load_config() -> dict:
    config_path = ROOT_DIR / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def collection_job():
    """Job 1: Thu thap RSS + Luu hang doi (khong AI)."""
    logger.info("")
    logger.info(f"[SCHEDULER] Bat dau JOB COLLECTION - {datetime.now().strftime('%H:%M:%S')}")
    try:
        run_collection()
    except Exception as e:
        logger.error(f"[SCHEDULER] Loi Collection Job: {e}")


def publish_job():
    """Job 2: Lay bai tot nhat tu hang doi, xu ly AI va dang bai."""
    logger.info("")
    logger.info(f"[SCHEDULER] Bat dau JOB PUBLISH - {datetime.now().strftime('%H:%M:%S')}")
    try:
        mode = os.getenv("OPERATION_MODE", "manual_review")
        run_publish(dry_run=False, circle_id="circle_tintuc24h")
    except Exception as e:
        logger.error(f"[SCHEDULER] Loi Publish Job: {e}")


def main():
    config = _load_config()
    collection_interval = config.get("collection", {}).get("interval_minutes", 20)
    publish_interval = config.get("publish_schedule", {}).get("interval_minutes", 45)
    active_start = config.get("publish_schedule", {}).get("active_hours_start", 6)
    active_end = config.get("publish_schedule", {}).get("active_hours_end", 23)

    logger.info("=" * 60)
    logger.info("  UCircle AI Feed Automation — Scheduler khoi dong")
    logger.info(f"  JOB 1 - Collection: moi {collection_interval} phut (0 AI calls)")
    logger.info(f"  JOB 2 - Publish:    moi {publish_interval} phut (khung {active_start}h-{active_end}h)")
    logger.info("  Nhan Ctrl+C de dung.")
    logger.info("=" * 60)

    # Chay ngay lan dau khi khoi dong
    logger.info("[SCHEDULER] Chay Collection lan dau...")
    collection_job()

    scheduler = BlockingScheduler(timezone="Asia/Ho_Chi_Minh")

    # Job 1 — Collection moi 20 phut, bat dau ngay
    scheduler.add_job(
        collection_job,
        trigger=IntervalTrigger(minutes=collection_interval),
        id="collection_job",
        name=f"RSS Collection (/{collection_interval}min)",
        replace_existing=True
    )

    # Job 2 — Publish moi 45 phut
    scheduler.add_job(
        publish_job,
        trigger=IntervalTrigger(minutes=publish_interval),
        id="publish_job",
        name=f"AI Publish (/{publish_interval}min, {active_start}h-{active_end}h)",
        replace_existing=True
    )

    logger.info(f"[SCHEDULER] 2 jobs dang chay. Tat de dung: Ctrl+C")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[SCHEDULER] Dung scheduler.")


if __name__ == "__main__":
    main()
