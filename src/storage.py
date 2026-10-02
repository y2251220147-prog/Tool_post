"""
src/storage.py
Database layer — SQLite với 6 bảng chính:
  sources, articles, stories, generated_posts, media_assets, publish_logs
"""

import sqlite3
import json
import os
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def get_db_path() -> str:
    """Lấy đường dẫn database từ config."""
    config_path = ROOT_DIR / "config" / "automation.json"
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    configured_path = Path(config["storage"]["db_path"])
    db_path = configured_path if configured_path.is_absolute() else ROOT_DIR / configured_path
    # Đảm bảo thư mục tồn tại
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


def get_connection() -> sqlite3.Connection:
    """Tạo kết nối SQLite với row_factory."""
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─────────────────────────────────────────────────────────────
# INIT DATABASE
# ─────────────────────────────────────────────────────────────

CREATE_TABLES_SQL = """
-- Bảng nguồn tin
CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    domain          TEXT NOT NULL,
    rss_urls        TEXT NOT NULL,       -- JSON array
    category        TEXT DEFAULT 'news',
    source_type     TEXT DEFAULT 'rss',
    trust_level     INTEGER DEFAULT 80,
    requires_attribution INTEGER DEFAULT 1,
    image_policy    TEXT DEFAULT 'DO_NOT_COPY',
    language        TEXT DEFAULT 'vi',
    enabled         INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- Bảng bài viết gốc thu thập từ RSS
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    source_name     TEXT NOT NULL,
    source_url      TEXT NOT NULL UNIQUE,
    original_title  TEXT NOT NULL,
    original_description TEXT,
    author          TEXT,
    published_at    TEXT,
    collected_at    TEXT DEFAULT (datetime('now','localtime')),
    category        TEXT,
    content_hash    TEXT,               -- hash của title+url để chặn trùng
    source_image_url TEXT,              -- ảnh đại diện lấy từ RSS hoặc bài gốc
    platform        TEXT DEFAULT 'news', -- news | youtube | facebook | facebook_reel
    media_type      TEXT DEFAULT 'article', -- article | video | reel | post
    external_id     TEXT,
    embed_url       TEXT,
    thumbnail_url   TEXT,
    license_type    TEXT DEFAULT 'unknown',
    rights_status   TEXT DEFAULT 'LINK_ONLY', -- REUSABLE_WITH_ATTRIBUTION | OWNED | LINK_ONLY
    status          TEXT DEFAULT 'NEW', -- NEW | QUEUED | PROCESSING | PROCESSED | REJECTED | DUPLICATE | EXPIRED | FAILED_ANALYSIS
    story_id        INTEGER,
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- Index cho deduplication
CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(source_url);
CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

-- Bảng story (nhóm bài cùng sự kiện)
CREATE TABLE IF NOT EXISTS stories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic           TEXT,
    main_title      TEXT,
    key_facts       TEXT,               -- JSON array
    confidence_score INTEGER DEFAULT 0,
    sensitivity_level TEXT DEFAULT 'low',  -- low | medium | high
    is_sensitive    INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'PENDING', -- PENDING | ANALYZED | READY | REJECTED
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- Bảng bài viết AI đã tạo
CREATE TABLE IF NOT EXISTS generated_posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        INTEGER REFERENCES stories(id),
    article_id      INTEGER REFERENCES articles(id),
    circle_id       TEXT NOT NULL,
    ai_title        TEXT,
    ai_summary      TEXT,
    bullet_points   TEXT,               -- JSON array
    hashtags        TEXT,               -- JSON array
    source_name     TEXT,
    source_url      TEXT,
    full_content    TEXT,               -- Nội dung bài đầy đủ
    relevance_score INTEGER DEFAULT 0,
    trust_score     INTEGER DEFAULT 0,
    freshness_score INTEGER DEFAULT 0,
    confidence_score INTEGER DEFAULT 0,
    ai_reason       TEXT,
    status          TEXT DEFAULT 'DRAFT', -- DRAFT | READY | REVIEW | REJECTED | PUBLISHED | FAILED
    reject_reason   TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON generated_posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_circle ON generated_posts(circle_id);

-- Hàng đợi tin đã được AI phân loại riêng cho từng Circle
CREATE TABLE IF NOT EXISTS circle_article_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    circle_id       TEXT NOT NULL,
    article_id      INTEGER NOT NULL REFERENCES articles(id),
    relevance_score INTEGER DEFAULT 0,
    ai_reason       TEXT,
    status          TEXT DEFAULT 'MATCHED', -- MATCHED | PROCESSING | GENERATED | SKIPPED
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(circle_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_circle_queue_circle_status
ON circle_article_queue(circle_id, status);
CREATE INDEX IF NOT EXISTS idx_circle_queue_article
ON circle_article_queue(article_id);

-- Bảng media assets (ảnh AI)
CREATE TABLE IF NOT EXISTS media_assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER REFERENCES generated_posts(id),
    type            TEXT DEFAULT 'image',   -- image | thumbnail
    image_prompt    TEXT,
    file_path       TEXT,
    file_name       TEXT,
    is_ai_generated INTEGER DEFAULT 1,
    source_url      TEXT,
    source_name     TEXT,
    sensitivity_note TEXT,
    validation_status TEXT DEFAULT 'PENDING', -- PENDING | PASS | REVIEW | REJECT
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- Bảng publish log
CREATE TABLE IF NOT EXISTS publish_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id         INTEGER REFERENCES generated_posts(id),
    circle_id       TEXT,
    platform        TEXT DEFAULT 'ucircle',
    publish_status  TEXT,               -- SUCCESS | FAILED | PENDING | RETRY
    platform_post_id TEXT,
    retry_count     INTEGER DEFAULT 0,
    error_message   TEXT,
    published_at    TEXT,
    created_at      TEXT DEFAULT (datetime('now','localtime'))
);

-- Bảng run logs (log theo từng lần chạy pipeline)
CREATE TABLE IF NOT EXISTS run_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT DEFAULT (datetime('now','localtime')),
    collected       INTEGER DEFAULT 0,
    url_duplicate   INTEGER DEFAULT 0,
    filtered        INTEGER DEFAULT 0,
    ai_analyzed     INTEGER DEFAULT 0,
    rejected        INTEGER DEFAULT 0,
    draft_generated INTEGER DEFAULT 0,
    review_pending  INTEGER DEFAULT 0,
    published       INTEGER DEFAULT 0,
    failed          INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0,
    notes           TEXT
);
"""


def init_database():
    """Khởi tạo database với tất cả bảng."""
    conn = get_connection()
    try:
        conn.executescript(CREATE_TABLES_SQL)
        _apply_safe_migrations(conn)
        conn.commit()
        from .logger import get_logger
        get_logger("storage").info(f"Database initialized: {get_db_path()}")
    finally:
        conn.close()


def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    """Lấy danh sách cột hiện có của một bảng SQLite."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str):
    """Bổ sung cột theo cách an toàn, không làm mất dữ liệu cũ."""
    if column_name not in _column_names(conn, table_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _image_worker_alive(process_id: int) -> bool:
    """Chỉ đọc trạng thái tiến trình, tuyệt đối không gửi tín hiệu kết thúc trên Windows."""
    if process_id == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, process_id)
        if not handle:
            return ctypes.get_last_error() != 87  # PID không tồn tại; lỗi quyền thì giữ khóa.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(process_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _recover_image_claims(conn: sqlite3.Connection, post_id: int | None = None):
    query = """SELECT id, image_work_owner,
                      updated_at < datetime('now', 'localtime', '-15 minutes') AS expired
               FROM generated_posts WHERE image_work_state = 'PROCESSING'"""
    rows = conn.execute(query + (" AND id = ?" if post_id is not None else ""),
                        (post_id,) if post_id is not None else ()).fetchall()
    for row in rows:
        owner = int(row["image_work_owner"] or 0)
        if (owner > 0 and not _image_worker_alive(owner)) or (owner == 0 and row["expired"]):
            conn.execute("""UPDATE generated_posts SET image_work_state = '', image_work_owner = 0
                            WHERE id = ? AND image_work_state = 'PROCESSING' AND image_work_owner = ?""",
                         (row["id"], owner))


def _apply_safe_migrations(conn: sqlite3.Connection):
    """Áp dụng các thay đổi cấu trúc dạng cộng thêm cho database hiện có."""
    _ensure_column(conn, "articles", "source_image_url", "TEXT")
    _ensure_column(conn, "articles", "updated_at", "TEXT")
    _ensure_column(conn, "articles", "platform", "TEXT DEFAULT 'news'")
    _ensure_column(conn, "articles", "media_type", "TEXT DEFAULT 'article'")
    _ensure_column(conn, "articles", "external_id", "TEXT")
    _ensure_column(conn, "articles", "embed_url", "TEXT")
    _ensure_column(conn, "articles", "thumbnail_url", "TEXT")
    _ensure_column(conn, "articles", "license_type", "TEXT DEFAULT 'unknown'")
    _ensure_column(conn, "articles", "rights_status", "TEXT DEFAULT 'LINK_ONLY'")
    _ensure_column(conn, "media_assets", "source_url", "TEXT")
    _ensure_column(conn, "media_assets", "source_name", "TEXT")
    _ensure_column(conn, "generated_posts", "image_analysis", "TEXT DEFAULT '{}'")
    _ensure_column(conn, "generated_posts", "image_work_state", "TEXT DEFAULT ''")
    _ensure_column(conn, "generated_posts", "image_work_owner", "INTEGER DEFAULT 0")
    for name, definition in {
        "provider": "TEXT DEFAULT ''", "model": "TEXT DEFAULT ''",
        "context_hash": "TEXT DEFAULT ''", "validation_reason": "TEXT DEFAULT ''",
        "relevance_score": "INTEGER DEFAULT 0", "generation_attempt": "INTEGER DEFAULT 0",
        "validation_result": "TEXT DEFAULT '{}'", "prompt_request": "TEXT DEFAULT ''",
        "is_active": "INTEGER DEFAULT 1",
    }.items():
        _ensure_column(conn, "media_assets", name, definition)
    _recover_image_claims(conn)
    conn.execute("""
        UPDATE articles
        SET updated_at = COALESCE(updated_at, collected_at, datetime('now','localtime'))
        WHERE updated_at IS NULL
    """)


# ─────────────────────────────────────────────────────────────
# SOURCES
# ─────────────────────────────────────────────────────────────

def upsert_source(source: dict):
    """Insert hoặc update một source."""
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO sources (id, name, domain, rss_urls, category, source_type,
                                 trust_level, requires_attribution, image_policy, language, enabled)
            VALUES (:id, :name, :domain, :rss_urls, :category, :source_type,
                    :trust_level, :requires_attribution, :image_policy, :language, :enabled)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                domain = excluded.domain,
                rss_urls = excluded.rss_urls,
                trust_level = excluded.trust_level,
                enabled = excluded.enabled,
                updated_at = datetime('now','localtime')
        """, {
            **source,
            "rss_urls": json.dumps(source.get("rss_urls", []), ensure_ascii=False),
            "requires_attribution": 1 if source.get("requires_attribution") else 0,
            "enabled": 1 if source.get("enabled") else 0,
        })
        conn.commit()
    finally:
        conn.close()


def get_enabled_sources() -> list[dict]:
    """Lấy tất cả sources đang bật."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM sources WHERE enabled = 1").fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["rss_urls"] = json.loads(d["rss_urls"])
            result.append(d)
        return result
    finally:
        conn.close()


def sync_sources_from_config() -> int:
    """Đồng bộ cấu hình nguồn RSS vào database theo cách idempotent."""
    sources_path = ROOT_DIR / "config" / "sources.json"
    with open(sources_path, encoding="utf-8") as f:
        sources = json.load(f).get("sources", [])
    for source in sources:
        upsert_source(source)
    return len(sources)


# ─────────────────────────────────────────────────────────────
# ARTICLES
# ─────────────────────────────────────────────────────────────

def url_exists(source_url: str) -> bool:
    """Kiểm tra URL đã tồn tại chưa (Level 1 duplicate)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE source_url = ?", (source_url,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def hash_exists(content_hash: str) -> bool:
    """Kiểm tra content_hash đã tồn tại chưa."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def find_article_by_identity(source_url: str = "", content_hash: str = "") -> dict | None:
    """Tìm bài đã có bằng URL hoặc hash để nguồn quét lại vẫn tái sử dụng được."""
    source_url = str(source_url or "").strip()
    content_hash = str(content_hash or "").strip()
    if not source_url and not content_hash:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT id, status, source_url, content_hash
            FROM articles
            WHERE (? != '' AND source_url = ?)
               OR (? != '' AND content_hash = ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (source_url, source_url, content_hash, content_hash),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_article(article: dict) -> int:
    """Lưu bài viết mới, trả về id."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT OR IGNORE INTO articles
                (source_id, source_name, source_url, original_title,
                 original_description, author, published_at, category,
                 content_hash, source_image_url, platform, media_type,
                 external_id, embed_url, thumbnail_url, license_type,
                 rights_status, status, updated_at)
            VALUES
                (:source_id, :source_name, :source_url, :original_title,
                 :original_description, :author, :published_at, :category,
                 :content_hash, :source_image_url, :platform, :media_type,
                 :external_id, :embed_url, :thumbnail_url, :license_type,
                 :rights_status, :status, datetime('now','localtime'))
        """, {
            "source_id": article.get("source_id", ""),
            "source_name": article.get("source_name", ""),
            "source_url": article.get("source_url", ""),
            "original_title": article.get("original_title", ""),
            "original_description": article.get("original_description", ""),
            "author": article.get("author", ""),
            "published_at": article.get("published_at", ""),
            "category": article.get("category", "news"),
            "content_hash": article.get("content_hash", ""),
            "source_image_url": article.get("source_image_url", ""),
            "platform": article.get("platform", "news"),
            "media_type": article.get("media_type", "article"),
            "external_id": article.get("external_id", ""),
            "embed_url": article.get("embed_url", ""),
            "thumbnail_url": article.get("thumbnail_url", ""),
            "license_type": article.get("license_type", "unknown"),
            "rights_status": article.get("rights_status", "LINK_ONLY"),
            "status": article.get("status", "NEW"),
        })
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_article_status(article_id: int, status: str, story_id: int = None):
    """Cập nhật trạng thái bài viết."""
    conn = get_connection()
    try:
        if story_id:
            conn.execute(
                "UPDATE articles SET status = ?, story_id = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (status, story_id, article_id)
            )
        else:
            conn.execute(
                "UPDATE articles SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (status, article_id)
            )
        conn.commit()
    finally:
        conn.close()


def update_article_source_url(article_id: int, source_url: str) -> bool:
    """Lưu URL báo gốc mà không làm lỗi khi URL đã thuộc bài khác."""
    if not str(source_url or "").startswith(("http://", "https://")):
        return False
    conn = get_connection()
    try:
        duplicate = conn.execute(
            "SELECT id FROM articles WHERE source_url = ? AND id != ?",
            (source_url, article_id),
        ).fetchone()
        if duplicate:
            return False
        conn.execute("""
            UPDATE articles
            SET source_url = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (source_url, article_id))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Một tiến trình khác có thể vừa lưu cùng URL sau bước kiểm tra trên.
        return False
    finally:
        conn.close()


def update_article_description(article_id: int, description: str) -> bool:
    """Cập nhật nội dung nguồn đã trích nếu đầy đủ hơn mô tả RSS hiện có."""
    cleaned = " ".join(str(description or "").split())[:8000]
    if len(cleaned) < 120:
        return False
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE articles
            SET original_description = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
              AND length(COALESCE(original_description, '')) < length(?)
        """, (cleaned, article_id, cleaned))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def published_source_url_exists(source_url: str) -> bool:
    """Kiểm tra URL nguồn thật đã từng được đăng thành công hay chưa."""
    if not str(source_url or "").startswith(("http://", "https://")):
        return False
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT 1
            FROM generated_posts p
            LEFT JOIN articles a ON a.id = p.article_id
            WHERE p.status = 'PUBLISHED'
              AND (p.source_url = ? OR a.source_url = ?)
            LIMIT 1
            """,
            (source_url, source_url),
        ).fetchone() is not None
    finally:
        conn.close()


def claim_article_for_processing(article_id: int) -> bool:
    """Nhận quyền xử lý một bài QUEUED theo cách nguyên tử để tránh chạy trùng."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE articles
            SET status = 'PROCESSING', updated_at = datetime('now','localtime')
            WHERE id = ? AND status = 'QUEUED'
        """, (article_id,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def recover_stale_processing_articles(stale_minutes: int = 30) -> int:
    """Khôi phục đồng bộ bài và hàng Circle bị kẹt ở PROCESSING."""
    conn = get_connection()
    try:
        cutoff = f"-{max(1, stale_minutes)} minutes"
        queue_cur = conn.execute("""
            UPDATE circle_article_queue
            SET status = 'MATCHED', updated_at = datetime('now','localtime')
            WHERE status = 'PROCESSING'
              AND updated_at < datetime('now', ?, 'localtime')
        """, (cutoff,))
        article_cur = conn.execute("""
            UPDATE articles
            SET status = 'QUEUED', updated_at = datetime('now','localtime')
            WHERE status = 'PROCESSING'
              AND COALESCE(updated_at, collected_at) < datetime('now', ?, 'localtime')
        """, (cutoff,))
        conn.commit()
        return max(0, article_cur.rowcount) + max(0, queue_cur.rowcount)
    finally:
        conn.close()


def enforce_queue_limit(max_queue_size: int = 50) -> int:
    """Giữ số bài QUEUED trong giới hạn và đánh dấu bài dư là EXPIRED."""
    limit = max(1, int(max_queue_size))
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT a.id
            FROM articles a
            LEFT JOIN sources s ON s.id = a.source_id
            WHERE a.status = 'QUEUED'
            ORDER BY COALESCE(s.trust_level, 0) DESC,
                     COALESCE(a.published_at, a.collected_at) DESC,
                     a.id DESC
            LIMIT -1 OFFSET ?
        """, (limit,)).fetchall()
        ids = [row[0] for row in rows]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE articles SET status = 'EXPIRED', updated_at = datetime('now','localtime') WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def get_recent_article_titles(days: int = 7) -> list[str]:
    """Lấy tiêu đề các bài gần đây để so sánh semantic duplicate."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT original_title FROM articles
            WHERE collected_at >= datetime('now', ?, 'localtime')
            AND status != 'REJECTED'
        """, (f"-{days} days",)).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def get_queued_article_count() -> int:
    """Dem so bai dang trong hang doi QUEUED."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE status = 'QUEUED'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_best_queued_article(expire_hours: int = 48) -> dict | None:
    """
    Chon bai tot nhat tu hang doi de dua vao AI Processing.
    Chien luoc: rank = trust_level * freshness (bai moi nhat, nguon uy tin nhat).
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT a.*, s.trust_level,
                   s.requires_attribution, s.image_policy,
                   (1.0 - (
                       (julianday('now','localtime') - julianday(a.published_at)) / :expire_days
                   )) AS freshness_score,
                   (s.trust_level * (1.0 - (
                       (julianday('now','localtime') - julianday(a.published_at)) / :expire_days
                   ))) AS rank_score
            FROM articles a
            JOIN sources s ON a.source_id = s.id
            WHERE a.status = 'QUEUED'
              AND a.collected_at >= datetime('now', :cutoff, 'localtime')
            ORDER BY rank_score DESC
            LIMIT 1
        """, {"expire_days": expire_hours / 24.0, "cutoff": f"-{expire_hours} hours"}).fetchone()

        return dict(row) if row else None
    finally:
        conn.close()


def expire_old_queued_articles(expire_hours: int = 48) -> int:
    """Danh dau EXPIRED cho bai QUEUED da qua han. Tra ve so bai bi expire."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE articles SET status = 'EXPIRED'
            WHERE status = 'QUEUED'
              AND collected_at < datetime('now', :cutoff, 'localtime')
        """, {"cutoff": f"-{expire_hours} hours"})
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_queue_stats() -> dict:
    """Thong ke nhanh hang doi hien tai (48h gan nhat)."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM articles
            WHERE collected_at >= datetime('now', '-48 hours', 'localtime')
            GROUP BY status
        """).fetchall()
        stats = {row[0]: row[1] for row in rows}
        return stats
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# CIRCLE ARTICLE QUEUE
# ─────────────────────────────────────────────────────────────

def recover_legacy_circle_rejections() -> int:
    """Chuyển lỗi phân loại kiểu cũ từ trạng thái toàn cục sang trạng thái từng Circle."""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE circle_article_queue
            SET status = 'REJECTED', updated_at = datetime('now','localtime')
            WHERE status IN ('SKIPPED', 'MATCHED', 'PROCESSING')
              AND article_id IN (
                  SELECT a.id FROM articles a WHERE a.status = 'REJECTED'
                  AND NOT EXISTS (
                      SELECT 1 FROM generated_posts p WHERE p.article_id = a.id
                  )
              )
        """)
        restored = conn.execute("""
            UPDATE articles
            SET status = 'QUEUED', updated_at = datetime('now','localtime')
            WHERE status = 'REJECTED'
              AND NOT EXISTS (
                  SELECT 1 FROM generated_posts p WHERE p.article_id = articles.id
              )
        """)
        conn.commit()
        return int(restored.rowcount or 0)
    finally:
        conn.close()


def save_circle_article_matches(circle_id: str, matches: list[dict]) -> int:
    """Lưu các tin AI đã phân loại cho một Circle, không tạo bản ghi trùng."""
    if not circle_id or not matches:
        return 0
    conn = get_connection()
    saved = 0
    try:
        for match in matches:
            try:
                article_id = int(match.get("article_id"))
                score = max(0, min(100, int(match.get("relevance_score", 0))))
            except (TypeError, ValueError):
                continue
            # Tin từng bị SKIPPED chỉ vì không phải lựa chọn số một vẫn được
            # phép quay lại ở lượt sau, miễn article còn QUEUED và không bị
            # Circle khác giữ/đã tạo.
            reactivated = conn.execute("""
                UPDATE circle_article_queue
                SET relevance_score = ?, ai_reason = ?, status = 'MATCHED',
                    updated_at = datetime('now','localtime')
                WHERE circle_id = ? AND article_id = ? AND status = 'SKIPPED'
                  AND EXISTS (
                      SELECT 1 FROM articles WHERE id = ? AND status = 'QUEUED'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM circle_article_queue other
                      WHERE other.article_id = ?
                        AND other.status IN ('MATCHED', 'PROCESSING', 'GENERATED')
                  )
            """, (
                score, str(match.get("reason", ""))[:1000], circle_id,
                article_id, article_id, article_id,
            ))
            if reactivated.rowcount:
                saved += reactivated.rowcount
                continue
            cur = conn.execute("""
                INSERT OR IGNORE INTO circle_article_queue
                    (circle_id, article_id, relevance_score, ai_reason, status)
                SELECT ?, ?, ?, ?, 'MATCHED'
                WHERE EXISTS (
                    SELECT 1 FROM articles WHERE id = ? AND status = 'QUEUED'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM circle_article_queue
                    WHERE article_id = ? AND status IN ('MATCHED', 'PROCESSING', 'GENERATED')
                )
            """, (
                circle_id, article_id, score, str(match.get("reason", ""))[:1000],
                article_id, article_id,
            ))
            saved += cur.rowcount
        conn.commit()
        return saved
    finally:
        conn.close()


def get_circle_article_queue(circle_id: str, status: str = "MATCHED", limit: int = 20) -> list[dict]:
    """Lấy hàng đợi tin đã phân loại của một Circle."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT q.id AS queue_id, q.circle_id, q.article_id,
                   q.relevance_score, q.ai_reason, q.status AS queue_status,
                   q.created_at AS matched_at,
                   a.original_title, a.original_description, a.source_url,
                   a.published_at, a.source_name, a.source_image_url,
                   COALESCE(a.platform, 'news') AS platform,
                   COALESCE(a.media_type, 'article') AS media_type,
                   COALESCE(a.license_type, 'unknown') AS license_type,
                   COALESCE(a.rights_status, 'LINK_ONLY') AS rights_status,
                   a.embed_url, a.thumbnail_url,
                   s.trust_level
            FROM circle_article_queue q
            JOIN articles a ON a.id = q.article_id
            LEFT JOIN sources s ON s.id = a.source_id
            WHERE q.circle_id = ? AND q.status = ? AND a.status = 'QUEUED'
            ORDER BY q.relevance_score DESC, q.id ASC
            LIMIT ?
        """, (circle_id, status, max(1, min(int(limit), 100)))).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def keep_only_best_circle_match(circle_id: str, preferred_article_id: int | None = None) -> int | None:
    """Giữ đúng một tin MATCHED tốt nhất, chuyển các tin dư sang SKIPPED."""
    conn = get_connection()
    try:
        preferred_id = int(preferred_article_id or 0)
        row = conn.execute("""
            SELECT q.article_id
            FROM circle_article_queue q
            JOIN articles a ON a.id = q.article_id
            WHERE q.circle_id = ? AND q.status = 'MATCHED' AND a.status = 'QUEUED'
            ORDER BY CASE WHEN q.article_id = ? THEN 0 ELSE 1 END,
                     q.relevance_score DESC,
                     a.published_at DESC,
                     q.id DESC
            LIMIT 1
        """, (circle_id, preferred_id)).fetchone()
        if not row:
            return None
        kept_id = int(row[0])
        conn.execute("""
            UPDATE circle_article_queue
            SET status = 'SKIPPED', updated_at = datetime('now','localtime')
            WHERE circle_id = ? AND status = 'MATCHED' AND article_id != ?
        """, (circle_id, kept_id))
        conn.commit()
        return kept_id
    finally:
        conn.close()


def claim_circle_article_match(circle_id: str, article_id: int) -> bool:
    """Khóa một tin đã phân loại trước khi tạo bài AI."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE circle_article_queue
            SET status = 'PROCESSING', updated_at = datetime('now','localtime')
            WHERE circle_id = ? AND article_id = ? AND status = 'MATCHED'
        """, (circle_id, article_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def update_circle_article_match_status(circle_id: str, article_id: int, status: str):
    """Cập nhật trạng thái tin trong hàng đợi riêng của Circle."""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE circle_article_queue
            SET status = ?, updated_at = datetime('now','localtime')
            WHERE circle_id = ? AND article_id = ?
        """, (status, circle_id, article_id))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# STORIES
# ─────────────────────────────────────────────────────────────

def save_story(story: dict) -> int:
    """Lưu story mới, trả về id."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO stories
                (topic, main_title, key_facts, confidence_score,
                 sensitivity_level, is_sensitive, status)
            VALUES
                (:topic, :main_title, :key_facts, :confidence_score,
                 :sensitivity_level, :is_sensitive, :status)
        """, {
            **story,
            "key_facts": json.dumps(story.get("key_facts", []), ensure_ascii=False),
            "is_sensitive": 1 if story.get("is_sensitive") else 0,
        })
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# GENERATED POSTS
# ─────────────────────────────────────────────────────────────

def save_post(post: dict) -> int:
    """Lưu bài đã được AI tạo, trả về id."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO generated_posts
                (story_id, article_id, circle_id, ai_title, ai_summary,
                 bullet_points, hashtags, source_name, source_url, full_content,
                 relevance_score, trust_score, freshness_score, confidence_score,
                 ai_reason, status, image_analysis)
            VALUES
                (:story_id, :article_id, :circle_id, :ai_title, :ai_summary,
                 :bullet_points, :hashtags, :source_name, :source_url, :full_content,
                 :relevance_score, :trust_score, :freshness_score, :confidence_score,
                 :ai_reason, :status, :image_analysis)
        """, {
            **post,
            "bullet_points": json.dumps(post.get("bullet_points", []), ensure_ascii=False),
            "hashtags": json.dumps(post.get("hashtags", []), ensure_ascii=False),
            "image_analysis": json.dumps(post.get("image_analysis") or {}, ensure_ascii=False),
        })
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_post_status(post_id: int, status: str, reject_reason: str = None):
    """Cập nhật trạng thái bài viết."""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE generated_posts SET status = ?, reject_reason = ?,
            updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (status, reject_reason, post_id))
        conn.commit()
    finally:
        conn.close()


def claim_post_for_publish(post_id: int) -> bool:
    """Khóa logic một bài READY trước khi đăng để ngăn đăng trùng."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            UPDATE generated_posts
            SET status = 'PUBLISHING', reject_reason = NULL,
                updated_at = datetime('now','localtime')
            WHERE id = ? AND status = 'READY'
        """, (post_id,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def update_post_full_content(post_id: int, full_content: str):
    """Cập nhật nội dung đầy đủ sau khi xác định loại ảnh thực tế."""
    conn = get_connection()
    try:
        conn.execute("""
            UPDATE generated_posts
            SET full_content = ?, updated_at = datetime('now','localtime')
            WHERE id = ?
        """, (full_content, post_id))
        conn.commit()
    finally:
        conn.close()


def _safe_parse_list(val) -> list:
    if isinstance(val, list):
        return val
    if not val:
        return []
    try:
        loaded = json.loads(val)
        if isinstance(loaded, list):
            return loaded
        return [str(loaded)]
    except Exception:
        s = str(val).strip()
        if s.startswith("[") and s.endswith("]"):
            import ast
            try:
                res = ast.literal_eval(s)
                if isinstance(res, list):
                    return res
            except Exception:
                pass
        return [s] if s else []


def get_posts_by_status(status: str, circle_id: str = None) -> list[dict]:
    """Lấy bài theo trạng thái."""
    conn = get_connection()
    try:
        if circle_id:
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
                WHERE p.status = ? AND p.circle_id = ?
                ORDER BY p.created_at DESC
            """, (status, circle_id)).fetchall()
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
                WHERE p.status = ?
                ORDER BY p.created_at DESC
            """, (status,)).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["bullet_points"] = _safe_parse_list(d.get("bullet_points"))
            d["hashtags"] = _safe_parse_list(d.get("hashtags"))
            result.append(d)
        return result
    finally:
        conn.close()


def get_post_by_id(post_id: int) -> dict | None:
    """Lấy bài theo id."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT p.*, m.file_path as image_path, m.image_prompt, m.file_name,
                   m.id as media_id, m.is_ai_generated, m.source_url as image_source_url,
                   m.source_name as image_source_name,
                   m.validation_status as image_validation_status, m.context_hash as image_context_hash,
                   m.provider as image_provider, m.model as image_model,
                   m.validation_reason as image_validation_reason, m.relevance_score as image_relevance_score
            FROM generated_posts p
            LEFT JOIN media_assets m ON m.id = (
                SELECT MAX(m2.id) FROM media_assets m2
                WHERE m2.post_id = p.id AND m2.is_active = 1
            )
            WHERE p.id = ?
        """, (post_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["bullet_points"] = _safe_parse_list(d.get("bullet_points"))
        d["hashtags"] = _safe_parse_list(d.get("hashtags"))
        return d
    finally:
        conn.close()


def get_all_media_assets_by_post(post_id: int) -> list[dict]:
    """Lấy tất cả media assets (ảnh) của một bài viết."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT * FROM media_assets
            WHERE post_id = ? AND file_name != '' AND validation_status = 'PASS' AND is_active = 1
            ORDER BY id ASC
        """, (post_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_published_source_urls() -> set:
    """Lấy tập hợp source_url của tất cả bài đã được PUBLISHED để tránh đăng lại."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT source_url FROM generated_posts
            WHERE status = 'PUBLISHED' AND source_url IS NOT NULL AND source_url != ''
        """).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def count_published_today(circle_id: str) -> int:
    """Đếm số bài đã publish hôm nay."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM publish_logs
            WHERE circle_id = ?
            AND publish_status = 'SUCCESS'
            AND date(published_at) = date('now', 'localtime')
        """, (circle_id,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def count_published_this_hour(circle_id: str) -> int:
    """Đếm số bài đã publish trong giờ hiện tại."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM publish_logs
            WHERE circle_id = ?
            AND publish_status = 'SUCCESS'
            AND strftime('%Y-%m-%d %H', published_at) = strftime('%Y-%m-%d %H', 'now', 'localtime')
        """, (circle_id,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_last_successful_publish_at(circle_id: str) -> str | None:
    """Lấy thời điểm đăng thành công gần nhất của một Circle."""
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT published_at FROM publish_logs
            WHERE circle_id = ? AND publish_status = 'SUCCESS' AND published_at IS NOT NULL
            ORDER BY published_at DESC LIMIT 1
        """, (circle_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# MEDIA ASSETS
# ─────────────────────────────────────────────────────────────

def save_media_asset(asset: dict) -> int:
    """Lưu lần xử lý ảnh mới; giữ lịch sử nhưng chỉ chọn lần hiện tại để đăng."""
    conn = get_connection()
    try:
        conn.execute("UPDATE media_assets SET is_active = 0 WHERE post_id = ?", (asset["post_id"],))
        cur = conn.execute("""
            INSERT INTO media_assets
                (post_id, type, image_prompt, file_path, file_name,
                 is_ai_generated, source_url, source_name,
                 sensitivity_note, validation_status, provider, model, context_hash,
                 validation_reason, relevance_score, generation_attempt, validation_result, prompt_request, is_active)
            VALUES
                (:post_id, :type, :image_prompt, :file_path, :file_name,
                 :is_ai_generated, :source_url, :source_name,
                 :sensitivity_note, :validation_status, :provider, :model, :context_hash,
                 :validation_reason, :relevance_score, :generation_attempt, :validation_result, :prompt_request, 1)
        """, {
            **asset,
            "source_url": asset.get("source_url", ""),
            "source_name": asset.get("source_name", ""),
            "provider": asset.get("provider", ""),
            "model": asset.get("model", ""),
            "context_hash": asset.get("context_hash", ""),
            "validation_reason": asset.get("validation_reason", ""),
            "relevance_score": asset.get("relevance_score", 0),
            "generation_attempt": asset.get("generation_attempt", 0),
            "validation_result": json.dumps(asset.get("validation_result") or {}, ensure_ascii=False),
            "prompt_request": asset.get("prompt_request", ""),
        })
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_latest_image_asset(post_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM media_assets WHERE post_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (post_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_post_for_image(post_id: int) -> bool:
    """Khóa công việc ảnh; chặn đăng đồng thời khi đang tạo hoặc kiểm tra lại."""
    conn = get_connection()
    try:
        _recover_image_claims(conn, post_id)
        cur = conn.execute("""
            UPDATE generated_posts SET image_work_state = 'PROCESSING', image_work_owner = ?, status = 'WAITING_IMAGE',
                updated_at = datetime('now', 'localtime')
            WHERE id = ? AND COALESCE(image_work_state, '') != 'PROCESSING'
              AND status NOT IN ('PUBLISHED', 'PUBLISHING')
        """, (os.getpid(), post_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release_post_image(post_id: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE generated_posts SET image_work_state = '', image_work_owner = 0 WHERE id = ?", (post_id,))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# PUBLISH LOGS
# ─────────────────────────────────────────────────────────────

def save_publish_log(log: dict) -> int:
    """Lưu publish log."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO publish_logs
                (post_id, circle_id, platform, publish_status,
                 platform_post_id, retry_count, error_message, published_at)
            VALUES
                (:post_id, :circle_id, :platform, :publish_status,
                 :platform_post_id, :retry_count, :error_message, :published_at)
        """, log)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────
# RUN LOGS
# ─────────────────────────────────────────────────────────────

def save_run_log(run: dict) -> int:
    """Lưu log một lần chạy pipeline."""
    conn = get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO run_logs
                (collected, url_duplicate, filtered, ai_analyzed,
                 rejected, draft_generated, review_pending, published, failed,
                 duration_seconds, notes)
            VALUES
                (:collected, :url_duplicate, :filtered, :ai_analyzed,
                 :rejected, :draft_generated, :review_pending, :published, :failed,
                 :duration_seconds, :notes)
        """, run)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_recent_run_logs(limit: int = 10) -> list[dict]:
    """Lấy các run log gần nhất."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM run_logs ORDER BY run_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
