"""
src/logger.py
Logging system — ghi log ra console và file
"""

import logging
import os
from datetime import datetime
from pathlib import Path
import json


def get_logger(name: str = "ucircle_automation") -> logging.Logger:
    """Tạo và trả về logger đã cấu hình."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Đã cấu hình rồi

    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    import sys
    try:
        if sys.stdout.encoding != 'utf-8':
            sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    # File handler — log theo ngày
    logs_dir = Path(__file__).parent.parent / "data" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


class RunStats:
    """Theo dõi thống kê một lần chạy pipeline."""

    def __init__(self):
        self.collected = 0
        self.url_duplicate = 0
        self.filtered = 0
        self.ai_analyzed = 0
        self.rejected = 0
        self.draft_generated = 0
        self.review_pending = 0
        self.published = 0
        self.failed = 0
        self.start_time = datetime.now()

    def duration_seconds(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()

    def to_dict(self) -> dict:
        return {
            "collected": self.collected,
            "url_duplicate": self.url_duplicate,
            "filtered": self.filtered,
            "ai_analyzed": self.ai_analyzed,
            "rejected": self.rejected,
            "draft_generated": self.draft_generated,
            "review_pending": self.review_pending,
            "published": self.published,
            "failed": self.failed,
            "duration_seconds": self.duration_seconds(),
            "notes": None,
        }

    def print_summary(self):
        logger = get_logger()
        logger.info("=" * 50)
        logger.info(f"📊 RUN SUMMARY — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        logger.info(f"  Collected:       {self.collected}")
        logger.info(f"  URL Duplicate:   {self.url_duplicate}")
        logger.info(f"  Filtered:        {self.filtered}")
        logger.info(f"  AI Analyzed:     {self.ai_analyzed}")
        logger.info(f"  Rejected:        {self.rejected}")
        logger.info(f"  Draft Generated: {self.draft_generated}")
        logger.info(f"  Review Pending:  {self.review_pending}")
        logger.info(f"  Published:       {self.published}")
        logger.info(f"  Failed:          {self.failed}")
        logger.info(f"  Duration:        {self.duration_seconds():.1f}s")
        logger.info("=" * 50)
