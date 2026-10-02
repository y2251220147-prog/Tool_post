import asyncio
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import analyzer, circle_scan_service, circle_selector, gemini_client, prompt_profiles, source_content, storage, ucircle_account, writer
from src import media_collector
from src.media_collector import _plain_search_query
from src.image_generator import (
    extract_embedded_page_urls,
    extract_source_image_url,
    replace_image_label,
)
from src.normalizer import normalize_article
from src.validator import validate_post
from src.publisher import (
    _publish_browser_headless,
    _normalize_verification_text,
    _verification_needles,
    _verification_snippet,
    get_last_publish_error,
    publish_post,
)
from src.queue_manager import can_publish_now
from src.source_resolver import is_google_news_url, resolve_source_url
from src.ucircle_account import _clean_circle_name, _remote_circle_id
from src.writer import _build_prompt as build_writer_prompt


class CoreWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test.db")
        self.db_patcher = patch("src.storage.get_db_path", return_value=self.db_path)
        self.db_patcher.start()
        storage.init_database()
        storage.upsert_source({
            "id": "source_test",
            "name": "Nguồn thử nghiệm",
            "domain": "example.com",
            "rss_urls": ["https://example.com/rss"],
            "category": "news",
            "source_type": "rss",
            "trust_level": 90,
            "requires_attribution": True,
            "image_policy": "ALLOW_WITH_ATTRIBUTION",
            "language": "vi",
            "enabled": True,
        })

    def tearDown(self):
        self.db_patcher.stop()
        self.temp_dir.cleanup()

    def _save_article(self, status="QUEUED"):
        return storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/news/1",
            "original_title": "Một tin tức thử nghiệm đủ dài để xử lý",
            "original_description": "Nội dung mô tả thử nghiệm về kinh tế và công nghệ.",
            "author": "Tác giả",
            "published_at": "2026-09-22 09:00:00",
            "category": "news",
            "content_hash": "hash-1",
            "source_image_url": "https://example.com/news.jpg",
            "status": status,
        })

    def test_normalizer_preserves_hot_score_and_source_image(self):
        normalized = normalize_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/news/2",
            "original_title": "Tin kinh tế và công nghệ mới nhất hôm nay",
            "original_description": "Mô tả bài viết.",
            "published_at": "2026-09-22 09:00:00",
            "content_hash": "hash-2",
            "source_image_url": "https://example.com/image.jpg",
            "_hot_score": 91.5,
        })
        self.assertEqual(normalized["_hot_score"], 91.5)
        self.assertEqual(normalized["source_image_url"], "https://example.com/image.jpg")

    def test_normalizer_and_storage_preserve_safe_video_metadata(self):
        normalized = normalize_article({
            "source_id": "source_test",
            "source_name": "YouTube thử nghiệm",
            "source_url": "https://www.youtube.com/watch?v=test123",
            "original_title": "Video thử nghiệm có tiêu đề đủ dài",
            "original_description": "Video về công nghệ và trí tuệ nhân tạo.",
            "published_at": "2026-09-22 09:00:00",
            "content_hash": "youtube-test123",
            "platform": "youtube",
            "media_type": "video",
            "external_id": "test123",
            "embed_url": "https://www.youtube.com/embed/test123",
            "thumbnail_url": "https://i.ytimg.com/test.jpg",
            "license_type": "creativeCommon",
            "rights_status": "REUSABLE_WITH_ATTRIBUTION",
        })
        article_id = storage.save_article(normalized)
        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT platform, media_type, license_type, rights_status FROM articles WHERE id = ?",
                (article_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["platform"], "youtube")
        self.assertEqual(row["media_type"], "video")
        self.assertEqual(row["license_type"], "creativeCommon")
        self.assertEqual(row["rights_status"], "REUSABLE_WITH_ATTRIBUTION")

    def test_youtube_collector_requires_official_api_key(self):
        with patch.dict("os.environ", {"YOUTUBE_API_KEY": ""}):
            result = media_collector.collect_youtube(
                {"circle_name": "Nét đẹp bóng đá"}, {"enabled": True}
            )
        self.assertEqual(result["status"], "missing_key")
        self.assertEqual(result["saved"], 0)

    def test_publish_browser_visibility_reads_dashboard_config_each_time(self):
        config_path = Path(self.temp_dir.name) / "automation.json"
        config_path.write_text(
            '{"browser": {"publish_headless": true}}', encoding="utf-8"
        )
        with patch("src.publisher.AUTOMATION_CONFIG_FILE", config_path):
            self.assertTrue(_publish_browser_headless())
            config_path.write_text(
                '{"browser": {"publish_headless": false}}', encoding="utf-8"
            )
            self.assertFalse(_publish_browser_headless())

    def test_platform_query_removes_google_or_syntax(self):
        query = _plain_search_query({
            "media_search_query": '"quán ăn Đà Nẵng" OR "đặc sản Đà Nẵng" OR "cafe Đà Nẵng"'
        })
        self.assertNotIn("OR", query)
        self.assertNotIn('"', query)
        self.assertIn("Đà Nẵng", query)

    def test_news_search_splits_or_query_into_focused_variants(self):
        queries = media_collector._search_queries({
            "media_search_query": (
                '"quán ăn Đà Nẵng" OR "đặc sản Đà Nẵng" OR '
                '"cafe Đà Nẵng"'
            )
        })
        self.assertEqual(queries, [
            "quán ăn Đà Nẵng",
            "đặc sản Đà Nẵng",
            "cafe Đà Nẵng",
        ])

    def test_general_news_query_expands_topics_instead_of_repeating_one_feed(self):
        queries = media_collector._search_queries({
            "circle_name": "Tin Tức 24h",
            "media_search_query": "tin tức Việt Nam mới nhất",
            "topics": ["thời sự", "kinh tế", "công nghệ"],
        })
        self.assertIn("thời sự Việt Nam mới nhất", queries)
        self.assertIn("kinh tế Việt Nam mới nhất", queries)
        self.assertIn("công nghệ Việt Nam mới nhất", queries)
        self.assertIn("tin tức Việt Nam mới nhất", queries)

    def test_youtube_query_rotates_one_focused_variant_per_scan(self):
        circle = {
            "circle_id": "circle_query_rotation_test",
            "media_search_query": '"mẹo dọn dẹp" OR "mẹo nấu ăn"',
        }
        media_collector._PLATFORM_QUERY_OFFSETS.pop(
            "youtube:circle_query_rotation_test", None
        )
        first = media_collector._next_platform_query(circle, "youtube")
        second = media_collector._next_platform_query(circle, "youtube")
        third = media_collector._next_platform_query(circle, "youtube")
        self.assertEqual((first, second, third), (
            "mẹo dọn dẹp", "mẹo nấu ăn", "mẹo dọn dẹp"
        ))

    def test_news_queries_rotate_in_separate_batches(self):
        circle = {
            "circle_id": "circle_tips_rotation_test",
            "media_search_variants": [f"mẹo thực hành {index}" for index in range(7)],
        }
        media_collector._PLATFORM_QUERY_OFFSETS.pop(
            "news:circle_tips_rotation_test", None
        )
        first = media_collector._next_news_queries(circle, batch_size=3)
        second = media_collector._next_news_queries(circle, batch_size=3)
        third = media_collector._next_news_queries(circle, batch_size=3)
        self.assertEqual(first, ["mẹo thực hành 0", "mẹo thực hành 1", "mẹo thực hành 2"])
        self.assertEqual(second, ["mẹo thực hành 3", "mẹo thực hành 4", "mẹo thực hành 5"])
        self.assertEqual(third, ["mẹo thực hành 6", "mẹo thực hành 0", "mẹo thực hành 1"])

    def test_source_content_prefers_json_ld_article_body(self):
        raw_html = """
        <html><head>
          <meta name="description" content="Mô tả ngắn của bài viết">
          <script type="application/ld+json">
            {"@type":"NewsArticle","articleBody":"Bước một là phân loại thực phẩm. Bước hai là đặt từng nhóm vào hộp kín. Bước ba là ghi ngày bảo quản để kiểm tra định kỳ."}
          </script>
        </head><body><p>Đoạn giới thiệu trên trang nguồn đủ dài để được đọc.</p></body></html>
        """
        extracted = source_content._extract_article_text(raw_html)
        self.assertIn("Bước một", extracted)
        self.assertIn("Bước ba", extracted)
        self.assertNotEqual(extracted, "Mô tả ngắn của bài viết")

    def test_source_content_ignores_footer_and_related_articles(self):
        raw_html = '''<html><head><title>Siêu phẩm sút xa của cầu thủ đội tuyển</title></head>
        <body><article><div class="article-content"><p>Cầu thủ ghi bàn từ cú sút xa,
        đưa bóng vào góc cao khung thành trong trận đấu vừa diễn ra.</p></div>
        <aside><p>Quyết định của Thủ tướng về cơ quan nhà nước và nền tảng số.</p></aside>
        <div class="related-news"><p>Tin thị trường kinh tế hoàn toàn khác chủ đề.</p></div>
        </article><footer><p>Quyết định của Thủ tướng về cơ quan nhà nước và nền tảng số.</p></footer></body></html>'''
        text = source_content._extract_article_text(raw_html, "Siêu phẩm sút xa của cầu thủ đội tuyển")
        self.assertIn("góc cao khung thành", text)
        self.assertNotIn("Thủ tướng", text)
        self.assertNotIn("thị trường", text)

    def test_source_content_does_not_overwrite_article_with_different_page(self):
        raw_html = '''<html><head><meta property="og:title" content="Quyết định của Chính phủ về nền tảng số"></head>
        <body><article><p>Nội dung về chính sách công nghệ không phải tình huống bóng đá.</p></article></body></html>'''
        self.assertEqual(source_content._extract_article_text(raw_html, "Top pha cứu thua World Cup"), "")

    def test_source_content_prefers_article_body_over_long_navigation_text(self):
        raw_html = '''<html><body><p>''' + "Tin không liên quan bên ngoài bài. " * 100 + '''</p>
        <article><p>Món bún chả cá tại Đà Nẵng được nguồn mô tả với nước dùng
        và nguyên liệu cụ thể, phù hợp để tóm tắt.</p></article></body></html>'''
        text = source_content._extract_article_text(raw_html)
        self.assertIn("bún chả cá", text)
        self.assertNotIn("không liên quan", text)

    def test_youtube_reads_full_metadata_without_inventing_reuse_rights(self):
        search_response = MagicMock()
        search_response.json.return_value = {"items": [{
            "id": {"videoId": "video_food"},
            "snippet": {"title": "Bún chả cá Đà Nẵng", "description": "Mô tả ngắn."},
        }]}
        details_response = MagicMock()
        full_description = "Bài nguồn giới thiệu bún chả cá và các nguyên liệu tại Đà Nẵng. " * 8
        details_response.json.return_value = {"items": [{
            "id": "video_food", "status": {"license": "youtube"},
            "snippet": {"title": "Bún chả cá Đà Nẵng", "description": full_description,
                        "channelTitle": "Nguồn ẩm thực", "publishedAt": "2026-09-28T09:00:00Z"},
        }]}
        with patch("src.media_collector.os.getenv", return_value="test_key"), patch(
            "src.media_collector.requests.get", side_effect=[search_response, details_response]
        ) as request_mock:
            result = media_collector.collect_youtube({
                "circle_id": "circle_food_metadata", "media_search_query": "bún chả cá Đà Nẵng",
                "media_max_age_days": 90,
            }, {"enabled": True})
        self.assertEqual(result["saved"], 1)
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[0].kwargs["params"]["order"], "relevance")
        self.assertIn("publishedAfter", request_mock.call_args_list[0].kwargs["params"])
        self.assertEqual(request_mock.call_args.args[0], "https://www.googleapis.com/youtube/v3/videos")
        conn = storage.get_connection()
        try:
            article = dict(conn.execute("SELECT * FROM articles WHERE external_id='video_food'").fetchone())
        finally:
            conn.close()
        self.assertEqual(article["original_description"], full_description)
        self.assertEqual(article["rights_status"], "LINK_ONLY")
        self.assertNotEqual(article["license_type"], "creativeCommon")

    def test_storage_only_replaces_description_with_longer_source_content(self):
        article_id = self._save_article()
        long_description = " ".join(["Hướng dẫn thực hiện an toàn theo từng bước cụ thể."] * 20)
        self.assertTrue(storage.update_article_description(article_id, long_description))
        self.assertFalse(storage.update_article_description(article_id, "Mô tả quá ngắn"))
        conn = storage.get_connection()
        try:
            saved = conn.execute(
                "SELECT original_description FROM articles WHERE id = ?", (article_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(saved, long_description)

    def test_source_content_recovers_from_invalid_response_encoding(self):
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "text/html"}
        response.encoding = "utf-8,gbk"
        response.apparent_encoding = "utf-8"
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [(
            "<html><body><p>Hướng dẫn gồm bước một chuẩn bị dụng cụ, "
            "bước hai thực hiện và bước ba kiểm tra an toàn.</p></body></html>"
        ).encode("utf-8")]
        with patch("src.source_content.requests.get", return_value=response):
            extracted = source_content.fetch_source_description("https://example.com/guide")
        self.assertIn("bước một", extracted)
        self.assertIn("kiểm tra an toàn", extracted)

    def test_news_search_collector_uses_circle_query_and_preserves_source(self):
        response = MagicMock()
        response.content = b"rss"
        response.raise_for_status.return_value = None
        entry = SimpleNamespace(
            link="https://news.google.com/articles/tip-1",
            title="Mẹo khử mùi thùng rác trong 30 giây - Báo thử nghiệm",
            summary="Hướng dẫn làm sạch đồ dùng trong gia đình.",
            published="Tue, 22 Sep 2026 09:00:00 GMT",
            id="tip-1",
            source={"title": "Báo thử nghiệm"},
        )
        with patch("src.media_collector.requests.get", return_value=response) as request_mock, patch(
            "feedparser.parse", return_value=SimpleNamespace(entries=[entry])
        ):
            result = media_collector.collect_news_search(
                {"media_search_query": "mẹo hay gia đình dọn dẹp"},
                {"enabled": True, "max_results_per_circle": 10, "max_age_days": 30},
            )
        self.assertEqual(result["saved"], 1)
        self.assertEqual(result["found"], 1)
        self.assertIn("when:30d", request_mock.call_args.kwargs["params"]["q"])
        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT original_title, source_name, platform, rights_status FROM articles WHERE external_id = ?",
                ("tip-1",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["original_title"], "Mẹo khử mùi thùng rác trong 30 giây")
        self.assertEqual(row["source_name"], "Báo thử nghiệm")
        self.assertEqual(row["platform"], "news")
        self.assertEqual(row["rights_status"], "LINK_ONLY")

    def test_news_collector_returns_existing_queued_article_as_current_candidate(self):
        response = MagicMock()
        response.content = b"rss"
        response.raise_for_status.return_value = None
        entry = SimpleNamespace(
            link="https://news.google.com/articles/reused-tip",
            title="Hướng dẫn bảo quản thực phẩm an toàn - Báo thử nghiệm",
            summary="Các bước bảo quản thực phẩm trong gia đình.",
            published="Tue, 22 Sep 2026 09:00:00 GMT",
            id="reused-tip",
            source={"title": "Báo thử nghiệm"},
        )
        with patch("src.media_collector.requests.get", return_value=response), patch(
            "feedparser.parse", return_value=SimpleNamespace(entries=[entry])
        ):
            first = media_collector.collect_news_search(
                {"media_search_query": "mẹo bảo quản thực phẩm"},
                {"enabled": True, "max_results_per_circle": 10, "max_age_days": 30},
            )
            second = media_collector.collect_news_search(
                {"media_search_query": "mẹo bảo quản thực phẩm"},
                {"enabled": True, "max_results_per_circle": 10, "max_age_days": 30},
            )
        self.assertEqual(first["saved"], 1)
        self.assertEqual(second["saved"], 0)
        self.assertEqual(second["reused"], 1)
        self.assertEqual(second["candidate_ids"], first["candidate_ids"])

    def test_article_claim_is_atomic(self):
        article_id = self._save_article()
        self.assertTrue(storage.claim_article_for_processing(article_id))
        self.assertFalse(storage.claim_article_for_processing(article_id))

    def test_recover_stale_processing_resets_article_and_circle_queue(self):
        article_id = self._save_article()
        storage.save_circle_article_matches("circle_food", [{
            "article_id": article_id,
            "relevance_score": 95,
            "reason": "Tin ẩm thực phù hợp",
        }])
        self.assertTrue(storage.claim_circle_article_match("circle_food", article_id))
        self.assertTrue(storage.claim_article_for_processing(article_id))
        conn = storage.get_connection()
        try:
            conn.execute(
                "UPDATE articles SET updated_at=datetime('now','-60 minutes','localtime') WHERE id=?",
                (article_id,),
            )
            conn.execute(
                "UPDATE circle_article_queue SET updated_at=datetime('now','-60 minutes','localtime') WHERE circle_id=? AND article_id=?",
                ("circle_food", article_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(storage.recover_stale_processing_articles(30), 2)
        conn = storage.get_connection()
        try:
            article_status = conn.execute(
                "SELECT status FROM articles WHERE id=?", (article_id,)
            ).fetchone()[0]
            queue_status = conn.execute(
                "SELECT status FROM circle_article_queue WHERE circle_id=? AND article_id=?",
                ("circle_food", article_id),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(article_status, "QUEUED")
        self.assertEqual(queue_status, "MATCHED")

    def test_resolved_source_url_collision_keeps_original_url(self):
        first_id = self._save_article()
        second_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Google News",
            "source_url": "https://news.google.com/articles/intermediate-2",
            "original_title": "Bài thứ hai dùng liên kết trung gian",
            "original_description": "Mô tả thử nghiệm.",
            "published_at": "2026-09-22 10:00:00",
            "category": "news",
            "content_hash": "hash-intermediate-2",
            "status": "QUEUED",
        })

        self.assertFalse(
            storage.update_article_source_url(second_id, "https://example.com/news/1")
        )
        conn = storage.get_connection()
        try:
            first_url = conn.execute(
                "SELECT source_url FROM articles WHERE id = ?", (first_id,)
            ).fetchone()[0]
            second_url = conn.execute(
                "SELECT source_url FROM articles WHERE id = ?", (second_id,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(first_url, "https://example.com/news/1")
        self.assertEqual(second_url, "https://news.google.com/articles/intermediate-2")

    def test_published_source_url_is_detected(self):
        article_id = self._save_article()
        conn = storage.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO generated_posts (article_id, circle_id, ai_title, source_url, status)
                VALUES (?, 'circle_food', 'Bài đã đăng', ?, 'PUBLISHED')
                """,
                (article_id, "https://example.com/news/1"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(storage.published_source_url_exists("https://example.com/news/1"))
        self.assertFalse(storage.published_source_url_exists("https://example.com/news/new"))

    def test_published_source_detects_canonical_article_url_when_post_keeps_old_url(self):
        article_id = self._save_article()
        conn = storage.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO generated_posts (article_id, circle_id, ai_title, source_url, status)
                VALUES (?, 'circle_food', 'Bài đã đăng', ?, 'PUBLISHED')
                """,
                (article_id, "https://news.google.com/articles/intermediate"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(storage.published_source_url_exists("https://example.com/news/1"))

    def test_auto_cycle_retries_failed_circles_before_long_rest(self):
        from review_ui import app as dashboard

        circle_ids = [f"circle_{index}" for index in range(1, 7)]
        scan_attempts = {circle_id: 0 for circle_id in circle_ids}
        published = []

        class FakeRequest:
            async def json(self):
                return {"interval_minutes": 30}

        def fake_scan(circle_id, control=None):
            scan_attempts[circle_id] += 1
            index = int(circle_id.rsplit("_", 1)[1])
            if index >= 4 and scan_attempts[circle_id] == 1:
                return {"success": False, "error": "Lỗi tạm thời"}
            return {
                "success": True,
                "post_id": index * 100 + scan_attempts[circle_id],
            }

        def fake_publish(post_id, max_retries=1, manual_override=False):
            published.append(post_id)
            return True

        def fake_sleep(_seconds):
            if dashboard._queue_publish_progress["stage"] == "cycle_wait":
                dashboard._queue_publish_cancel = True

        discovered = [
            {
                "local_circle_id": circle_id,
                "scan_enabled": True,
                "publish_enabled": True,
            }
            for circle_id in circle_ids
        ]
        dashboard._bg_status["queue_publish"] = False
        dashboard._queue_publish_cancel = False

        with patch.object(dashboard, "get_discovered_circles", return_value=discovered), patch.object(
            dashboard,
            "get_circle_config",
            side_effect=lambda circle_id: {"circle_name": circle_id},
        ), patch.object(dashboard, "get_posts_by_status", return_value=[]), patch.object(
            dashboard, "_scan_and_generate_for_circle", side_effect=fake_scan
        ), patch.object(
            dashboard,
            "get_post_by_id",
            side_effect=lambda post_id: {"id": post_id, "status": "READY"},
        ), patch.object(dashboard, "publish_post", side_effect=fake_publish), patch.object(
            dashboard, "_save_publish_checkpoint"
        ), patch.object(
            dashboard, "_archive_publish_progress"
        ), patch(
            "time.sleep", side_effect=fake_sleep
        ):
            response = asyncio.run(dashboard.publish_queue(FakeRequest()))
            deadline = time.monotonic() + 5
            while dashboard._bg_status["queue_publish"] and time.monotonic() < deadline:
                threading.Event().wait(0.01)

        self.assertEqual(response["status"], "started")
        self.assertFalse(dashboard._bg_status["queue_publish"])
        self.assertEqual(len(published), 6)
        self.assertEqual(dashboard._queue_publish_progress["succeeded"], 6)
        self.assertEqual(dashboard._queue_publish_progress["failed"], 0)
        self.assertEqual(dashboard._queue_publish_progress["pending"], 0)
        self.assertEqual([scan_attempts[circle_id] for circle_id in circle_ids[:3]], [1, 1, 1])
        self.assertEqual([scan_attempts[circle_id] for circle_id in circle_ids[3:]], [2, 2, 2])
        self.assertTrue(any("đủ 6/6 Circle" in line for line in dashboard._queue_publish_progress["log"]))

    def test_publish_checkpoint_roundtrip_and_pause_controls(self):
        from review_ui import app as dashboard

        checkpoint_path = Path(self.temp_dir.name) / "publish-checkpoint.json"
        original_progress = dict(dashboard._queue_publish_progress)
        try:
            with patch.object(dashboard, "QUEUE_CHECKPOINT_PATH", checkpoint_path):
                dashboard._queue_publish_progress.update({
                    "mode": "resumable_batch_v4",
                    "state": "stopped",
                    "stage": "stopped",
                    "cycle": 2,
                    "attempt": 3,
                    "total": 6,
                    "succeeded": 4,
                    "pending": 2,
                    "selected_circle_ids": [f"circle_{index}" for index in range(1, 7)],
                    "successful_circle_ids": [f"circle_{index}" for index in range(1, 5)],
                    "pending_circle_ids": ["circle_5", "circle_6"],
                })
                dashboard._save_publish_checkpoint()
                loaded = dashboard._load_publish_checkpoint()
                self.assertEqual(loaded["successful_circle_ids"], [
                    "circle_1", "circle_2", "circle_3", "circle_4"
                ])
                self.assertEqual(loaded["pending_circle_ids"], ["circle_5", "circle_6"])

                dashboard._bg_status["queue_publish"] = True
                dashboard._queue_publish_pause = False
                pause_response = asyncio.run(dashboard.pause_publish_queue())
                self.assertEqual(pause_response["status"], "pausing")
                self.assertTrue(dashboard._queue_publish_pause)

                resume_response = asyncio.run(dashboard.resume_publish_queue())
                self.assertEqual(resume_response["status"], "resumed")
                self.assertFalse(dashboard._queue_publish_pause)

                stop_response = asyncio.run(dashboard.stop_publish_queue())
                self.assertEqual(stop_response["status"], "stopping")
                self.assertTrue(dashboard._queue_publish_cancel)
        finally:
            dashboard._bg_status["queue_publish"] = False
            dashboard._queue_publish_pause = False
            dashboard._queue_publish_cancel = False
            dashboard._queue_publish_resume_event.set()
            dashboard._queue_publish_progress.clear()
            dashboard._queue_publish_progress.update(original_progress)

    def test_publish_history_upserts_and_deletes_one_run(self):
        from review_ui import app as dashboard

        history_path = Path(self.temp_dir.name) / "publish-history.json"
        progress = {
            "run_id": "run_test_001",
            "started_at": "2026-09-24T08:00:00",
            "ended_at": "2026-09-24T08:10:00",
            "state": "stopped",
            "cycle": 1,
            "attempt": 2,
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "pending": 1,
            "selected_circle_ids": ["circle_1", "circle_2"],
            "successful_circle_ids": ["circle_1"],
            "pending_circle_ids": ["circle_2"],
            "published_post_ids": [101],
            "log": ["Đã đăng Circle 1", "Circle 2 đang chờ thử lại"],
        }

        with patch.object(dashboard, "PUBLISH_HISTORY_PATH", history_path):
            dashboard._archive_publish_progress(progress)
            history = dashboard._load_publish_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["run_id"], "run_test_001")
            self.assertEqual(history[0]["published_post_ids"], [101])

            progress.update({
                "ended_at": "2026-09-24T08:12:00",
                "state": "finished",
                "succeeded": 2,
                "failed": 0,
                "pending": 0,
                "successful_circle_ids": ["circle_1", "circle_2"],
                "pending_circle_ids": [],
                "published_post_ids": [101, 102],
            })
            dashboard._archive_publish_progress(progress)
            history = dashboard._load_publish_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["state"], "finished")
            self.assertEqual(history[0]["succeeded"], 2)
            self.assertEqual(history[0]["published_post_ids"], [101, 102])

            response = asyncio.run(dashboard.delete_queue_publish_history("run_test_001"))
            self.assertTrue(response["success"])
            self.assertEqual(response["removed"], 1)
            self.assertEqual(dashboard._load_publish_history(), [])

    def test_circle_article_queue_is_separate_and_atomic(self):
        article_id = self._save_article()
        saved = storage.save_circle_article_matches("circle_a", [{
            "article_id": article_id,
            "relevance_score": 91,
            "reason": "Đúng chủ đề Circle A",
        }])
        self.assertEqual(saved, 1)
        self.assertEqual(len(storage.get_circle_article_queue("circle_a")), 1)
        self.assertEqual(
            storage.save_circle_article_matches("circle_b", [{
                "article_id": article_id,
                "relevance_score": 88,
                "reason": "Không được đăng chéo",
            }]),
            0,
        )
        self.assertTrue(storage.claim_circle_article_match("circle_a", article_id))
        self.assertFalse(storage.claim_circle_article_match("circle_a", article_id))
        storage.update_circle_article_match_status("circle_a", article_id, "MATCHED")
        self.assertEqual(len(storage.get_circle_article_queue("circle_a")), 1)

    def test_circle_rejection_does_not_remove_article_from_other_circles(self):
        article_id = self._save_article()
        storage.save_circle_article_matches("circle_a", [{
            "article_id": article_id,
            "relevance_score": 80,
            "reason": "Thử thẩm định cho Circle A",
        }])
        storage.update_circle_article_match_status("circle_a", article_id, "SKIPPED")
        storage.update_article_status(article_id, "REJECTED")

        self.assertEqual(storage.recover_legacy_circle_rejections(), 1)
        candidates_a = circle_selector._queued_candidates(
            12, ["news"], circle_id="circle_a"
        )
        candidates_b = circle_selector._queued_candidates(
            12, ["news"], circle_id="circle_b"
        )
        self.assertNotIn(article_id, [item["id"] for item in candidates_a])
        self.assertIn(article_id, [item["id"] for item in candidates_b])

    def test_legacy_rejection_recovers_circle_match_stuck_in_processing(self):
        article_id = self._save_article()
        storage.save_circle_article_matches("circle_a", [{
            "article_id": article_id, "relevance_score": 80, "reason": "Thử thẩm định",
        }])
        self.assertTrue(storage.claim_circle_article_match("circle_a", article_id))
        storage.update_article_status(article_id, "REJECTED")

        self.assertEqual(storage.recover_legacy_circle_rejections(), 1)
        conn = storage.get_connection()
        try:
            row = conn.execute(
                "SELECT status FROM circle_article_queue WHERE circle_id = ? AND article_id = ?",
                ("circle_a", article_id),
            ).fetchone()
            self.assertEqual(row[0], "REJECTED")
        finally:
            conn.close()
        self.assertIn(article_id, [item["id"] for item in
            circle_selector._queued_candidates(12, ["news"], circle_id="circle_b")])

    def test_matched_article_recovers_both_queues_after_unexpected_generation_error(self):
        from review_ui import app as dashboard

        article_id = self._save_article()
        storage.save_circle_article_matches("circle_a", [{
            "article_id": article_id, "relevance_score": 90, "reason": "Tin thử nghiệm",
        }])

        def fail_generation(selected_id, _circle_id, control=None):
            storage.claim_article_for_processing(selected_id)
            raise RuntimeError("Lỗi tạo bài thử nghiệm")

        with patch.object(dashboard, "process_single_article", side_effect=fail_generation):
            result = dashboard._generate_matched_article("circle_a", article_id)

        self.assertFalse(result["success"])
        self.assertIn("Lỗi tạo bài tạm thời", result["error"])
        self.assertEqual(storage.get_circle_article_queue("circle_a")[0]["article_id"], article_id)
        self.assertTrue(storage.claim_article_for_processing(article_id))

    def test_scan_and_generate_moves_past_rejected_article_and_creates_only_one_post(self):
        from review_ui import app as dashboard

        first_id = self._save_article()
        second_id = storage.save_article({
            "source_id": "source_test", "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/national-policy",
            "original_title": "Chính phủ công bố chính sách y tế áp dụng toàn quốc",
            "original_description": "Dữ kiện chính sách mới có tác động tới người dân.",
            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content_hash": "national-policy-test", "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_a", "circle_name": "Circle thử nghiệm",
            "scan_enabled": True, "ownership_verified": True,
            "content_prompt": "Chọn thông tin hữu ích và có nguồn rõ ràng.",
        }

        def process(selected_id, _circle_id, control=None):
            self.assertTrue(storage.claim_article_for_processing(selected_id))
            if selected_id == first_id:
                storage.update_article_status(selected_id, "REJECTED")
                return {
                    "success": False, "error_code": "article_not_suitable",
                    "error": "Gemini đánh giá bài chưa phù hợp để đăng (81/100): Tin cấp xã thường kỳ.",
                }
            storage.update_article_status(selected_id, "PROCESSED")
            return {"success": True, "post_id": 9001, "title": "Chính sách y tế mới", "status": "READY"}

        with patch.object(dashboard, "get_circle_config", return_value=circle), patch(
            "src.circle_selector.get_circle_config", return_value=circle
        ), patch("src.media_collector.collect_media_for_circle", return_value={
            "candidate_ids": [first_id, second_id], "sources": [],
        }), patch("src.circle_selector.generate_json", side_effect=[
            {"matches": [{"article_id": first_id, "relevance_score": 85}]},
            {"matches": [{"article_id": second_id, "relevance_score": 95}]},
        ]), patch.object(dashboard, "process_single_article", side_effect=process) as process_mock:
            result = dashboard._scan_and_generate_for_circle("circle_a")

        self.assertTrue(result["success"])
        self.assertEqual(result["post_id"], 9001)
        self.assertEqual(result["generated"], 1)
        self.assertEqual(process_mock.call_count, 2)
        self.assertEqual([item["success"] for item in result["selection_attempts"]], [False, True])
        self.assertNotIn(first_id, [item["id"] for item in
            circle_selector._queued_candidates(12, ["news"], circle_id="circle_a")])
        self.assertIn(first_id, [item["id"] for item in
            circle_selector._queued_candidates(12, ["news"], circle_id="circle_b")])

    def test_scan_preserves_selector_error_without_trying_to_generate_missing_article(self):
        from review_ui import app as dashboard

        with patch.object(dashboard, "get_circle_config", return_value={"circle_name": "Circle thử nghiệm"}), patch(
            "src.media_collector.collect_media_for_circle", return_value={"sources": []}
        ), patch("src.circle_selector.scan_articles_for_circle", return_value={
            "success": False, "error": "Circle chưa bật tùy chọn Quét bài.",
        }), patch.object(dashboard, "_generate_matched_article") as generation_mock:
            result = dashboard._scan_and_generate_for_circle("circle_a")

        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Circle chưa bật tùy chọn Quét bài.")
        generation_mock.assert_not_called()

    def test_circle_scan_uses_own_prompt_and_saves_matches(self):
        article_id = self._save_article()
        circle = {
            "circle_id": "circle_bong_da",
            "circle_name": "Nét đẹp bóng đá",
            "ownership_verified": True,
            "scan_enabled": True,
            "content_prompt": "Chỉ chọn tin bóng đá.",
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json",
            return_value={"matches": [{
                "article_id": article_id,
                "relevance_score": 93,
                "reason": "Tin bóng đá phù hợp",
            }]},
        ) as generate_mock:
            result = circle_selector.scan_articles_for_circle("circle_bong_da")
        self.assertTrue(result["success"])
        self.assertEqual(result["matched"], 1)
        self.assertIn("Chỉ chọn tin bóng đá", generate_mock.call_args.args[0])
        queued = storage.get_circle_article_queue("circle_bong_da")
        self.assertEqual(queued[0]["article_id"], article_id)

    def test_circle_scan_empty_strict_candidates_does_not_call_gemini(self):
        self._save_article()
        circle = {
            "circle_id": "circle_food", "circle_name": "Đà Nẵng ăn gì",
            "ownership_verified": True, "scan_enabled": True,
            "content_prompt": "Chọn món ăn tại Đà Nẵng.",
            "fallback_filter": {"all_groups": [["đà nẵng"], ["quán ăn", "bún chả cá"]]},
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json"
        ) as generate_mock:
            result = circle_selector.scan_articles_for_circle("circle_food")
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["selection_mode"], "local_no_match")
        self.assertIn("bộ lọc chủ đề/địa điểm", result["message"])
        generate_mock.assert_not_called()

    def test_circle_candidates_prioritize_current_collection_before_sql_pool_limit(self):
        target_id = self._save_article()
        conn = storage.get_connection()
        try:
            conn.execute("UPDATE articles SET collected_at='2000-01-01' WHERE id=?", (target_id,))
            conn.executemany('''INSERT INTO articles
                (source_id,source_name,source_url,original_title,status,category)
                VALUES ('source_test','Nguồn thử nghiệm',?,?,'QUEUED','circle_search')''',
                [(f"https://example.com/pool/{index}", f"Tin thời sự khác {index}") for index in range(310)])
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn(target_id, [row["id"] for row in circle_selector._queued_candidates(12)])
        candidates = circle_selector._queued_candidates(12, preferred_article_ids=[target_id])
        self.assertEqual(candidates[0]["id"], target_id)

    def test_external_food_filter_accepts_named_dishes_and_rejects_trade_fairs(self):
        circle = prompt_profiles.resolve_circle_prompt({"circle_name": "Đà Nẵng ăn gì", "prompt_profile_id": "da_nang_an_gi"})
        candidates = [
            {"id": 1, "original_title": "Bún chả cá Đà Nẵng: nét đặc trưng của món ăn", "original_description": "Nguồn mô tả nguyên liệu và cách ăn."},
            {"id": 2, "original_title": "Đà Nẵng quảng bá Food Tour tại hội chợ TP Hồ Chí Minh"},
            {"id": 3, "original_title": "Đà Nẵng mở không gian văn hóa ẩm thực tại Hội An"},
            {"id": 4, "original_title": "Bún chả cá Nha Trang thơm ngon"},
        ]
        matches = circle_selector._local_keyword_matches(circle, candidates, 10)
        self.assertEqual([item["article_id"] for item in matches], [1])
        self.assertEqual(circle["media_max_age_days"], 90)

    def test_external_football_filter_prioritizes_actual_goals_not_generic_comebacks(self):
        circle = prompt_profiles.resolve_circle_prompt({"circle_name": "Nét đẹp bóng đá hay", "prompt_profile_id": "net_dep_bong_da_hay"})
        candidates = [
            {"id": 1, "original_title": "Khoảnh khắc đặc biệt khi cầu thủ tái xuất đội tuyển"},
            {"id": 2, "original_title": "Liverpool ghi bàn bằng siêu phẩm sút xa"},
            {"id": 3, "original_title": "Tổng hợp bàn thắng Ngoại Hạng Anh 23/24"},
            {"id": 4, "original_title": "Pha bóng cứu thua bóng chuyền đẹp"},
        ]
        matches = circle_selector._local_keyword_matches(circle, candidates, 10)
        self.assertEqual([item["article_id"] for item in matches], [2])

    def test_football_filter_prefers_readable_report_over_sparse_video_compilation(self):
        circle = prompt_profiles.resolve_circle_prompt({"circle_name": "Nét đẹp bóng đá hay", "prompt_profile_id": "net_dep_bong_da_hay"})
        candidates = [
            {"id": 1, "original_title": "Top 10 bàn thắng đẹp nhất bóng đá thế giới", "source_name": "VTVgo", "original_description": "Video tổng hợp."},
            {"id": 2, "original_title": "Tiến Linh ghi siêu phẩm bàn thắng bằng đầu tại V League", "source_name": "Bongda24h", "original_description": "Bài báo thuật lại bàn thắng."},
            {"id": 3, "original_title": "Siêu phẩm bàn thắng vòng cuối FVPL Summer", "source_name": "VTVgo", "original_description": "Giải đấu thể thao điện tử."},
        ]
        matches = circle_selector._local_keyword_matches(circle, candidates, 5)
        self.assertEqual([item["article_id"] for item in matches], [2, 1])

    def test_scan_failure_keeps_last_rejection_reason_when_next_selection_is_empty(self):
        from review_ui import app as dashboard
        article_id = self._save_article()
        circle = {"circle_name": "Circle thử nghiệm"}
        with patch.object(dashboard, "get_circle_config", return_value=circle), patch(
            "src.media_collector.collect_media_for_circle", return_value={"sources": []}
        ), patch("src.circle_selector.scan_articles_for_circle", side_effect=[
            {"success": True, "selected_article_id": article_id},
            {"success": True, "message": "Chưa có tin phù hợp.", "matched": 0},
        ]), patch.object(dashboard, "keep_only_best_circle_match", side_effect=[article_id, None]), patch.object(
            dashboard, "_generate_matched_article", return_value={
                "success": False, "error_code": "article_not_suitable", "error": "Nguồn thiếu diễn biến pha bóng.",
            }
        ):
            result = dashboard._scan_and_generate_for_circle("circle_test")
        self.assertFalse(result["success"])
        self.assertIn("Nguồn thiếu diễn biến pha bóng", result["message"])

    def test_circle_scan_always_keeps_only_one_hottest_article(self):
        first_id = self._save_article()
        second_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/news/second",
            "original_title": "Bàn thắng đẹp trong trận cầu mới nhất",
            "original_description": "Tình huống bóng đá nổi bật.",
            "published_at": "2026-09-23 08:00:00",
            "category": "thể thao",
            "content_hash": "second-hot-article",
            "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_bong_da",
            "circle_name": "Nét đẹp bóng đá",
            "ownership_verified": True,
            "scan_enabled": True,
            "content_prompt": "Chỉ chọn tin bóng đá nổi bật.",
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json",
            return_value={"matches": [
                {"article_id": second_id, "relevance_score": 96, "reason": "Tin hot nhất"},
                {"article_id": first_id, "relevance_score": 90, "reason": "Tin phù hợp thứ hai"},
            ]},
        ):
            result = circle_selector.scan_articles_for_circle(
                "circle_bong_da", limit=20, max_matches=5
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["selected_article_id"], second_id)
        queued = storage.get_circle_article_queue("circle_bong_da")
        self.assertEqual([item["article_id"] for item in queued], [second_id])

    def test_circle_candidates_exclude_exact_title_already_published(self):
        article_id = self._save_article()
        conn = storage.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO generated_posts (article_id, circle_id, ai_title, status)
                VALUES (?, 'circle_food', ?, 'PUBLISHED')
                """,
                (article_id, "Một tin tức thử nghiệm đủ dài để xử lý"),
            )
            conn.commit()
        finally:
            conn.close()

        candidates = circle_selector._queued_candidates(12, ["news"])
        self.assertNotIn(article_id, [item["id"] for item in candidates])

    def test_circle_candidates_exclude_near_duplicate_published_story(self):
        published_id = self._save_article()
        duplicate_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn khác",
            "source_url": "https://example.com/news/same-story-another-source",
            "original_title": "Một tin tức thử nghiệm đủ dài để xử lý hôm nay",
            "original_description": "Cùng sự kiện nhưng nguồn đã thay đổi tiêu đề nhẹ.",
            "published_at": "2026-09-23 10:00:00",
            "category": "news",
            "content_hash": "near-duplicate-story",
            "status": "QUEUED",
        })
        conn = storage.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO generated_posts (article_id, circle_id, ai_title, source_url, status)
                VALUES (?, 'circle_news', 'Tin thử nghiệm đã được đăng', ?, 'PUBLISHED')
                """,
                (published_id, "https://example.com/news/1"),
            )
            conn.commit()
        finally:
            conn.close()
        candidates = circle_selector._queued_candidates(12, ["news"])
        self.assertNotIn(duplicate_id, [item["id"] for item in candidates])

    def test_specialized_circle_never_sends_unmatched_candidates_to_ai(self):
        circle = {
            "media_search_query": '"mẹo dọn dẹp" OR "mẹo nấu ăn"',
            "fallback_filter": {
                "all_groups": [
                    ["mẹo", "hướng dẫn", "các bước"],
                    ["dọn dẹp", "nấu ăn", "bảo quản"],
                ],
                "exclude": ["đời tư"],
            },
        }
        candidates = [
            {
                "id": 1,
                "original_title": "Hướng dẫn các bước bảo quản thực phẩm an toàn",
                "original_description": "Mẹo dùng hộp kín trong căn bếp gia đình.",
                "category": "đời sống",
            },
            {
                "id": 2,
                "original_title": "Câu chuyện hôn nhân của một nghệ sĩ nổi tiếng",
                "original_description": "Thông tin đời tư và tình cảm.",
                "category": "giải trí",
            },
        ]
        prioritized = circle_selector._prioritize_for_ai(circle, candidates, 12)
        self.assertEqual([item["id"] for item in prioritized], [1])

    def test_circle_queue_discards_extra_matches_without_deleting_them(self):
        first_id = self._save_article()
        second_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/news/keep",
            "original_title": "Tin nổi bật cần giữ lại",
            "original_description": "Mô tả tin nổi bật.",
            "published_at": "2026-09-23 09:00:00",
            "category": "tin tức",
            "content_hash": "keep-only-best",
            "status": "QUEUED",
        })
        storage.save_circle_article_matches("circle_a", [
            {"article_id": first_id, "relevance_score": 88, "reason": "Tin cũ"},
            {"article_id": second_id, "relevance_score": 96, "reason": "Tin hot"},
        ])

        kept_id = storage.keep_only_best_circle_match("circle_a", second_id)

        self.assertEqual(kept_id, second_id)
        self.assertEqual(
            [item["article_id"] for item in storage.get_circle_article_queue("circle_a")],
            [second_id],
        )
        reactivated = storage.save_circle_article_matches("circle_a", [{
            "article_id": first_id,
            "relevance_score": 97,
            "reason": "Được chọn lại ở lượt sau",
        }])
        self.assertEqual(reactivated, 1)
        self.assertIn(
            first_id,
            [item["article_id"] for item in storage.get_circle_article_queue("circle_a")],
        )

    def test_circle_scan_falls_back_to_local_keywords_when_gemini_is_busy(self):
        article_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/football/1",
            "original_title": "Đội tuyển bóng đá Việt Nam công bố lịch thi đấu mới",
            "original_description": "Thông tin cầu thủ và chiến thuật trước trận đấu.",
            "author": "Tác giả",
            "published_at": "2026-09-22 09:00:00",
            "category": "thể thao",
            "content_hash": "football-hash-1",
            "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_bong_da",
            "circle_name": "Nét đẹp bóng đá",
            "ownership_verified": True,
            "scan_enabled": True,
            "media_platforms": ["news"],
            "media_search_query": "bóng đá đội tuyển cầu thủ chiến thuật trận đấu",
            "content_prompt": "Chỉ chọn tin bóng đá.",
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json",
            side_effect=RuntimeError("503 UNAVAILABLE"),
        ):
            result = circle_selector.scan_articles_for_circle("circle_bong_da")

        self.assertTrue(result["success"])
        self.assertEqual(result["selection_mode"], "local_fallback")
        self.assertEqual(result["matched"], 1)
        self.assertEqual(storage.get_circle_article_queue("circle_bong_da")[0]["article_id"], article_id)

    def test_circle_scan_uses_strict_local_match_when_gemini_returns_empty(self):
        article_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/football/resilience",
            "original_title": "Nghị lực trở lại sân cỏ của cầu thủ đội tuyển Việt Nam",
            "original_description": "Câu chuyện bóng đá về hành trình vượt khó và tinh thần fair play.",
            "published_at": "2026-09-23 09:00:00",
            "category": "thể thao",
            "content_hash": "football-local-after-empty",
            "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_bong_da",
            "circle_name": "Nét đẹp bóng đá",
            "ownership_verified": True,
            "scan_enabled": True,
            "media_platforms": ["news"],
            "media_search_query": "bóng đá fair play nghị lực cầu thủ",
            "content_prompt": "Chỉ chọn câu chuyện đẹp trong bóng đá.",
            "fallback_filter": {
                "all_groups": [
                    ["bóng đá", "cầu thủ", "đội tuyển"],
                    ["fair play", "nghị lực", "vượt khó"],
                ],
                "exclude": ["cá cược"],
            },
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json", return_value={"matches": []}
        ):
            result = circle_selector.scan_articles_for_circle("circle_bong_da")

        self.assertTrue(result["success"])
        self.assertEqual(result["selection_mode"], "local_strict")
        self.assertEqual(result["selected_article_id"], article_id)
        self.assertEqual(result["matched"], 1)

    def test_specialized_circle_selects_strict_local_match_without_spending_selector_ai(self):
        article_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/tips/clean-home",
            "original_title": "Bí quyết dọn dẹp nhà cửa gọn gàng trong 30 phút",
            "original_description": "Các bước thực tế dễ áp dụng mỗi ngày.",
            "published_at": "2026-09-23 10:00:00",
            "category": "đời sống",
            "content_hash": "strict-tip-no-selector-ai",
            "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_meo_hay",
            "circle_name": "Mẹo hay mỗi ngày",
            "ownership_verified": True,
            "scan_enabled": True,
            "media_platforms": ["news"],
            "media_search_query": "mẹo gia đình dọn dẹp",
            "content_prompt": "Chỉ chọn mẹo thực tế.",
            "fallback_filter": {
                "all_groups": [
                    ["mẹo", "bí quyết", "hướng dẫn"],
                    ["dọn dẹp", "nhà cửa", "làm sạch"],
                ],
                "exclude": ["đời tư"],
            },
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json"
        ) as generate_json_mock:
            result = circle_selector.scan_articles_for_circle("circle_meo_hay")

        self.assertTrue(result["success"])
        self.assertEqual(result["selection_mode"], "local_strict")
        self.assertEqual(result["selected_article_id"], article_id)
        generate_json_mock.assert_not_called()

    def test_circle_fallback_checks_beyond_first_forty_articles(self):
        for index in range(45):
            storage.save_article({
                "source_id": "source_test",
                "source_name": "Nguồn thử nghiệm",
                "source_url": f"https://example.com/unrelated/{index}",
                "original_title": f"Tin thời sự không liên quan {index}",
                "original_description": "Nội dung xã hội tổng hợp.",
                "published_at": f"2026-09-22 10:{index:02d}:00",
                "category": "xã hội",
                "content_hash": f"unrelated-{index}",
                "status": "QUEUED",
            })
        target_id = storage.save_article({
            "source_id": "source_test",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/tips/deep-result",
            "original_title": "Mẹo làm sạch căn bếp theo từng bước",
            "original_description": "Hướng dẫn vệ sinh nhà cửa an toàn.",
            "published_at": "2026-09-20 08:00:00",
            "category": "đời sống",
            "content_hash": "deep-tip-result",
            "status": "QUEUED",
        })
        circle = {
            "circle_id": "circle_meo_hay",
            "circle_name": "Mẹo hay mỗi ngày",
            "ownership_verified": True,
            "scan_enabled": True,
            "media_platforms": ["news"],
            "media_search_query": "mẹo gia đình dọn dẹp nấu ăn",
            "content_prompt": "Chỉ chọn mẹo gia đình hữu ích.",
            "fallback_filter": {
                "all_groups": [["mẹo", "hướng dẫn"], ["làm sạch", "vệ sinh", "bếp"]],
                "exclude": ["thời sự"],
            },
        }
        with patch("src.circle_selector.get_circle_config", return_value=circle), patch(
            "src.circle_selector.generate_json", side_effect=RuntimeError("503 UNAVAILABLE")
        ):
            result = circle_selector.scan_articles_for_circle("circle_meo_hay", limit=20)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(storage.get_circle_article_queue("circle_meo_hay")[0]["article_id"], target_id)

    def test_local_tip_filter_requires_instruction_and_life_topic(self):
        circle = {
            "circle_id": "circle_meo_hay",
            "circle_name": "Mẹo hay mỗi ngày",
            "media_search_query": "mẹo hay gia đình dọn dẹp nấu ăn gia dụng",
            "fallback_filter": {
                "all_groups": [
                    ["mẹo", "cách làm", "hướng dẫn", "các bước"],
                    ["dọn dẹp", "nấu ăn", "gia dụng", "bảo quản"],
                ],
                "exclude": ["thời sự", "bảo hiểm", "lãi suất"],
            },
        }
        candidates = [
            {
                "id": 1,
                "original_title": "Bảo đảm an toàn công trình thủy lợi trước mưa lớn",
                "original_description": "Tin thời sự trong ngày.",
                "category": "xã hội",
                "platform": "news",
            },
            {
                "id": 2,
                "original_title": "Hướng dẫn cách bảo quản thực phẩm trong tủ lạnh",
                "original_description": "Các bước gia dụng dễ áp dụng tại nhà.",
                "category": "đời sống",
                "platform": "youtube",
            },
        ]
        matches = circle_selector._local_keyword_matches(circle, candidates, 5)
        self.assertEqual([item["article_id"] for item in matches], [2])

    def test_local_tip_filter_rejects_match_found_only_in_rss_description(self):
        circle = {
            "circle_id": "circle_meo_hay",
            "media_search_query": "mẹo gia đình",
            "fallback_filter": {
                "all_groups": [
                    ["mẹo", "hướng dẫn", "cách"],
                    ["dọn dẹp", "ứng dụng", "bảo quản"],
                ],
                "exclude": [],
            },
        }
        candidates = [{
            "id": 99,
            "original_title": "Tiêu điểm quốc tế và chiến lược tại Bắc Cực",
            "original_description": "Ứng dụng cung cấp hướng dẫn theo dõi tin tức.",
            "category": "thời sự",
            "platform": "news",
        }]
        matches = circle_selector._local_keyword_matches(circle, candidates, 5)
        self.assertEqual(matches, [])

    def test_retired_gemini_model_is_not_used_as_fallback(self):
        with patch("src.gemini_client._load_ai_config", return_value={
            "model_text": "gemini-3.5-flash-lite",
            "fallback_models": ["gemini-3.8-flash", "gemini-3.6-flash"],
        }), patch("src.gemini_client._available_text_models", return_value=(
            "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash",
        )):
            candidates = gemini_client._model_candidates("test-key")
        self.assertNotIn("gemini-2.5-flash", candidates)

    def test_gemini_circuit_breaker_skips_network_after_quota_error(self):
        try:
            gemini_client._open_circuit(60, "hết hạn mức API")
            with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch(
                "src.gemini_client._create_client"
            ) as create_client:
                with self.assertRaisesRegex(RuntimeError, "chế độ dự phòng"):
                    gemini_client.generate_json("Trả về JSON", purpose="kiểm thử")
            create_client.assert_not_called()
        finally:
            with gemini_client._GEMINI_STATE_LOCK:
                gemini_client._GEMINI_BLOCKED_UNTIL = 0.0
                gemini_client._GEMINI_BLOCK_REASON = ""

    def test_analyzer_uses_safe_local_fallback_when_quota_is_exhausted(self):
        article = {
            "original_title": "Hướng dẫn cách bảo quản thực phẩm trong tủ lạnh",
            "original_description": (
                "Bài viết hướng dẫn các bước sắp xếp và bảo quản thực phẩm trong tủ lạnh. "
                "Người dùng cần kiểm tra nhiệt độ và đóng kín thực phẩm trước khi lưu trữ."
            ),
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/tips/1",
            "published_at": "2026-09-22 09:00:00",
            "category": "đời sống",
            "trust_level": 90,
        }
        with patch("src.analyzer.generate_json", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")) as generate_mock:
            result = analyzer.analyze_article(article)
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(result["analysis_mode"], "local_fallback")
        self.assertTrue(result["publish"])
        self.assertGreaterEqual(len(result["key_facts"]), 2)

    def test_analyzer_prompt_uses_target_circle_instead_of_fixed_news_circle(self):
        article = {
            "original_title": "Một món ngon tại Đà Nẵng",
            "original_description": "Bài viết giới thiệu món ăn và địa chỉ tại Đà Nẵng.",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/da-nang-food",
        }
        prompt = analyzer._build_prompt(article, circle={
            "circle_name": "Đà Nẵng ăn gì",
            "content_prompt": "Chỉ chọn quán ăn và món ngon nằm tại Đà Nẵng.",
        })
        self.assertIn('Circle "Đà Nẵng ăn gì"', prompt)
        self.assertIn("Chỉ chọn quán ăn và món ngon nằm tại Đà Nẵng.", prompt)
        self.assertIn("overall_score >= 70", prompt)
        self.assertNotIn('Circle "Tin Tức 24h"', prompt)

        news_prompt = analyzer._build_prompt(article, circle={
            "local_circle_id": "circle_tintuc24h",
            "circle_name": "Tin Tức 24h",
            "content_prompt": "Chọn tin thời sự đã kiểm chứng.",
        })
        self.assertIn("overall_score >= 85", news_prompt)

        tips_prompt = analyzer._build_prompt(article, circle={
            "circle_name": "Mẹo hay mỗi ngày",
            "content_prompt": "Chọn lời khuyên gia đình có thể áp dụng.",
            "analysis_publish_threshold": 60,
        })
        self.assertIn("overall_score >= 60", tips_prompt)

    def test_analyzer_applies_explicit_circle_threshold_to_safe_practical_tip(self):
        article = {
            "original_title": "Lời khuyên cho gia đình dùng bình nóng lạnh",
            "original_description": "Bài nguồn đưa ra các lưu ý sử dụng thiết bị và tiết kiệm điện an toàn.",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/tips/water-heater",
            "category": "đời sống",
        }
        ai_result = {
            "topic": "đời sống",
            "relevance_score": 70,
            "trust_score": 70,
            "freshness_score": 55,
            "content_quality_score": 60,
            "overall_score": 62,
            "is_sensitive": False,
            "sensitivity_level": "low",
            "publish": False,
            "reason": "Nội dung là lời khuyên thực tế nhưng độ mới không cao.",
            "key_facts": ["Tắt thiết bị khi không sử dụng."],
            "suggested_hashtags": ["#MeoGiaDinh"],
        }
        with patch("src.analyzer.generate_json", return_value=ai_result):
            result = analyzer.analyze_article(article, circle={
                "circle_name": "Mẹo hay mỗi ngày",
                "analysis_publish_threshold": 60,
                "analysis_allow_threshold_override": True,
            })
        self.assertTrue(result["publish"])
        self.assertIn("ngưỡng riêng 60/100", result["reason"])

    def test_analyzer_does_not_repeat_after_timeout(self):
        article = {
            "original_title": "Kinh nghiệm ăn uống tại Đà Nẵng",
            "original_description": (
                "Nguồn chia sẻ địa chỉ, món ăn và kinh nghiệm tham khảo khi khám phá "
                "ẩm thực tại Đà Nẵng, kèm thông tin cần kiểm tra trước khi đến."
            ),
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/da-nang-timeout",
        }
        with patch(
            "src.analyzer.generate_json",
            side_effect=RuntimeError("504 DEADLINE_EXCEEDED: The read operation timed out"),
        ) as generate_mock:
            result = analyzer.analyze_article(article, max_retries=3, circle={
                "circle_name": "Đà Nẵng ăn gì",
                "content_prompt": "Chọn nội dung ẩm thực tại Đà Nẵng.",
            })
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(result["analysis_mode"], "local_fallback")

    def test_writer_creates_reviewable_fallback_draft_when_gemini_is_busy(self):
        article = {
            "original_title": "Hướng dẫn bảo quản rau củ đúng cách",
            "original_description": "Bài viết cung cấp các bước bảo quản rau củ trong ngăn mát.",
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/tips/2",
        }
        analysis = {
            "topic": "đời sống",
            "key_facts": [
                "Rau củ cần được phân loại trước khi bảo quản.",
                "Người dùng cần kiểm tra hướng dẫn trong bài gốc.",
            ],
            "overall_score": 78,
            "analysis_mode": "local_fallback",
        }
        with patch("src.writer._get_circle_config", return_value={
            "circle_name": "Mẹo hay mỗi ngày",
            "display_name": "Mẹo hay mỗi ngày",
            "content_prompt": "Chỉ chọn mẹo hữu ích.",
        }), patch("src.writer.generate_json", side_effect=RuntimeError("503 UNAVAILABLE")) as generate_mock:
            result = writer.write_post(article, analysis, circle_id="circle_meo_hay")
        self.assertEqual(generate_mock.call_count, 1)
        self.assertEqual(result["generation_mode"], "local_fallback")
        self.assertIn("https://example.com/tips/2", result["full_content"])
        self.assertGreaterEqual(len(result["bullet_points"]), 3)
        self.assertEqual(len(result["hashtags"]), 5)
        self.assertIn("🖼 Ảnh từ bài gốc", result["full_content"])
        self.assertNotIn("bản tóm tắt dự phòng", result["full_content"])

    def test_writer_rebuilds_structure_and_keeps_exactly_five_relevant_hashtags(self):
        article = {
            "original_title": "Bàn thắng đẹp của đội tuyển Việt Nam",
            "original_description": "Cầu thủ ghi bàn bằng cú sút xa trong trận đấu mới nhất.",
            "source_name": "VTV",
            "source_url": "https://example.com/football/goal",
        }
        analysis = {
            "topic": "bóng đá",
            "key_facts": ["Bàn thắng được ghi bằng một cú sút xa."],
            "overall_score": 92,
            "suggested_hashtags": ["#BongDaVietNam"],
        }
        circle = {
            "circle_name": "Nét đẹp bóng đá hay",
            "display_name": "Nét đẹp bóng đá hay",
            "content_prompt": "Chọn khoảnh khắc bóng đá đẹp.",
            "hashtag_seeds": ["#BanThangDep", "#KhoanhKhacBongDa", "#FairPlay"],
        }
        ai_result = {
            "ai_title": "Cú sút xa tạo nên bàn thắng đẹp cho đội tuyển Việt Nam",
            "ai_summary": " ".join(["nội dung"] * 80),
            "bullet_points": ["Cú sút xa tạo nên bàn thắng nổi bật."],
            "hashtags": ["#Khác", "#BongDa", "#BanThangDep", "#DoiTuyenVietNam", "#TheThao", "#QuaNhieu"],
            "full_content": "Nội dung sai cấu trúc từ AI",
        }
        with patch("src.writer._get_circle_config", return_value=circle), patch(
            "src.writer.generate_json", return_value=ai_result
        ), patch("src.writer.time.sleep"):
            result = writer.write_post(article, analysis, circle_id="circle_bong_da")

        self.assertEqual(len(result["hashtags"]), 5)
        self.assertNotIn("#Khác", result["hashtags"])
        self.assertIn("📌 Điểm chính:", result["full_content"])
        self.assertIn("🔗 Xem chi tiết:", result["full_content"])
        self.assertIn("📰 Nguồn: VTV", result["full_content"])
        self.assertIn("🖼 Ảnh từ bài gốc — Nguồn: VTV", result["full_content"])

    def test_bulk_circle_scan_uses_three_workers_and_keeps_partial_failures(self):
        active = 0
        max_active = 0
        lock = threading.Lock()

        def fake_scan(circle_id):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.04)
            with lock:
                active -= 1
            if circle_id == "circle_4":
                return {
                    "circle_id": circle_id,
                    "circle_name": circle_id,
                    "success": False,
                    "matched": 0,
                    "media_saved": 0,
                    "error": "Gemini tạm quá tải",
                }
            return {
                "circle_id": circle_id,
                "circle_name": circle_id,
                "success": True,
                "matched": 1,
                "media_saved": 2,
                "message": "Đã quét",
            }

        circle_ids = [f"circle_{index}" for index in range(6)]
        with patch("src.circle_scan_service._scan_one_circle", side_effect=fake_scan):
            result = circle_scan_service.scan_circles_parallel(circle_ids, max_workers=3)

        self.assertEqual(max_active, 3)
        self.assertEqual(result["total"], 6)
        self.assertEqual(result["succeeded"], 5)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["matched"], 5)
        self.assertEqual(result["media_saved"], 10)
        self.assertFalse(result["success"])

    def test_post_query_does_not_duplicate_when_many_images_exist(self):
        article_id = self._save_article(status="PROCESSED")
        story_id = storage.save_story({
            "topic": "công nghệ",
            "main_title": "Tin thử nghiệm",
            "key_facts": [],
            "confidence_score": 90,
            "sensitivity_level": "low",
            "is_sensitive": False,
            "status": "ANALYZED",
        })
        post_id = storage.save_post({
            "story_id": story_id,
            "article_id": article_id,
            "circle_id": "circle_tintuc24h",
            "ai_title": "Tiêu đề bài viết thử nghiệm",
            "ai_summary": "Đây là phần tóm tắt đủ dài dùng cho bài kiểm tra. " * 8,
            "bullet_points": ["Ý 1", "Ý 2"],
            "hashtags": ["#TinTuc", "#CongNghe"],
            "source_name": "Nguồn thử nghiệm",
            "source_url": "https://example.com/news/1",
            "full_content": "Nội dung",
            "relevance_score": 90,
            "trust_score": 90,
            "freshness_score": 90,
            "confidence_score": 90,
            "ai_reason": "Đạt",
            "status": "READY",
        })
        for index in range(2):
            storage.save_media_asset({
                "post_id": post_id,
                "type": "image",
                "image_prompt": "",
                "file_path": f"image-{index}.jpg",
                "file_name": f"image-{index}.jpg",
                "is_ai_generated": 0,
                "source_url": "https://example.com/image.jpg",
                "source_name": "Nguồn thử nghiệm",
                "sensitivity_note": "",
                "validation_status": "PASS",
            })
        posts = storage.get_posts_by_status("READY")
        self.assertEqual(len(posts), 1)
        self.assertTrue(storage.claim_post_for_publish(post_id))
        self.assertFalse(storage.claim_post_for_publish(post_id))

    def test_extracts_open_graph_image_and_updates_label(self):
        html = '<html><head><meta property="og:image" content="/media/main.jpg"></head></html>'
        image_url = extract_source_image_url(html, "https://example.com/news/1")
        self.assertEqual(image_url, "https://example.com/media/main.jpg")
        content = replace_image_label("Nội dung\n🖼 Ảnh minh họa AI\n#Tin", "🖼 Ảnh từ bài gốc — Nguồn: Báo A")
        self.assertIn("Ảnh từ bài gốc", content)
        self.assertNotIn("Ảnh minh họa AI", content)

    def test_publish_verification_uses_article_title(self):
        content = "🔥 Bàn thắng đẹp nhất vòng đấu\n\nNội dung bài viết\n📌 Điểm chính:"
        self.assertEqual(_verification_snippet(content), "🔥 Bàn thắng đẹp nhất vòng đấu")

    def test_publish_verification_tolerates_ui_spacing_and_punctuation(self):
        content = "🎭 Đà Nẵng ra mắt show nghệ thuật: lịch sử, di sản và ẩm thực"
        rendered = "ĐÀ NẴNG  ra mắt show nghệ thuật — lịch sử, di sản và ẩm thực"
        normalized_page = _normalize_verification_text(rendered)
        self.assertTrue(
            any(needle in normalized_page for needle in _verification_needles(content))
        )

    def test_source_image_ignores_logo_and_reads_video_poster(self):
        article_url = "https://example.com/news/goal"
        article_html = """
            <html><head><meta property="og:image" content="/img/logo.png"></head>
            <body><iframe src="/video-embed/123.html"></iframe></body></html>
        """
        video_html = '<video poster="/media/beautiful-goal.jpg"></video>'

        self.assertEqual(extract_source_image_url(article_html, article_url), "")
        self.assertEqual(
            extract_embedded_page_urls(article_html, article_url),
            ["https://example.com/video-embed/123.html"],
        )
        self.assertEqual(
            extract_source_image_url(
                video_html, "https://example.com/video-embed/123.html"
            ),
            "https://example.com/media/beautiful-goal.jpg",
        )

    def test_source_resolver_only_opens_google_news_links(self):
        google_url = "https://news.google.com/rss/articles/encoded-id?oc=5"
        publisher_url = "https://cafef.vn/bai-viet.chn"
        self.assertTrue(is_google_news_url(google_url))
        self.assertFalse(is_google_news_url(publisher_url))
        self.assertEqual(resolve_source_url(publisher_url), publisher_url)

    def test_ucircle_circle_url_and_display_name_are_normalized(self):
        url = "https://ucircle.net/app/c/ceaf7e7a-1826-4571-97b7-350efe750733"
        self.assertEqual(
            _remote_circle_id(url),
            "ceaf7e7a-1826-4571-97b7-350efe750733",
        )
        self.assertEqual(
            _remote_circle_id("https://example.com/app/c/fake-circle"),
            "",
        )
        self.assertEqual(
            _clean_circle_name(
                "C Cộng Đồng Pickleball Việt Nam 2 thành viên · 1 bài",
                "Circle dự phòng",
            ),
            "Cộng Đồng Pickleball Việt Nam",
        )

    def test_owned_circle_defaults_are_safe_and_settings_are_independent(self):
        config_path = Path(self.temp_dir.name) / "circles.json"
        discovered_path = Path(self.temp_dir.name) / "discovered.json"
        config_path.write_text('{"circles": []}', encoding="utf-8")
        with patch.object(ucircle_account, "CIRCLES_CONFIG_FILE", config_path), patch.object(
            ucircle_account, "DISCOVERED_CIRCLES_FILE", discovered_path
        ):
            circles = ucircle_account._sync_owned_circle_configs([{
                "remote_id": "e19c1e0a-69c0-4657-83c6-187df3b3565f",
                "circle_name": "Quà tặng cuộc sống",
                "circle_url": "https://ucircle.net/app/c/e19c1e0a-69c0-4657-83c6-187df3b3565f",
                "role": "owner",
                "relationship": "owned",
                "format": "fanpage",
                "category": "other",
                "category_name": "Lĩnh vực khác",
                "member_count": 10,
            }])
            circle = circles[0]
            self.assertFalse(circle["scan_enabled"])
            self.assertFalse(circle["publish_enabled"])
            self.assertEqual(circle["prompt_profile_id"], "qua_tang_cuoc_song")
            updated = ucircle_account.update_circle_settings(
                circle["local_circle_id"],
                scan_enabled=True,
                publish_enabled=False,
                content_prompt="Chỉ chọn những câu chuyện nhân văn và hữu ích.",
                display_name="Quà tặng cuộc sống - chọn lọc",
            )
            self.assertTrue(updated["scan_enabled"])
            self.assertFalse(updated["publish_enabled"])
            self.assertEqual(updated["display_name"], "Quà tặng cuộc sống - chọn lọc")
            self.assertEqual(updated["prompt_source"], "circle_override")
            hidden = ucircle_account.set_circle_dashboard_visibility(
                circle["local_circle_id"], False
            )
            self.assertFalse(hidden["dashboard_visible"])
            self.assertFalse(hidden["scan_enabled"])
            self.assertFalse(hidden["publish_enabled"])
            restored = ucircle_account.set_circle_dashboard_visibility(
                circle["local_circle_id"], True
            )
            self.assertTrue(restored["dashboard_visible"])

    def test_prompt_profiles_match_name_and_fall_back_safely(self):
        matched = prompt_profiles.match_prompt_profile("Mẹo hay mỗi ngày", "Lĩnh vực khác")
        self.assertEqual(matched["profile_id"], "meo_hay_moi_ngay")

        food = prompt_profiles.match_prompt_profile("Ẩm thực Đà Nẵng", "Ăn uống")
        self.assertEqual(food["profile_id"], "da_nang_an_gi")

        generic = prompt_profiles.resolve_circle_prompt({
            "circle_name": "Circle hoàn toàn mới",
            "ucircle_category_name": "Chủ đề chưa biết",
        })
        self.assertEqual(generic["prompt_profile_id"], "generic")
        self.assertIn("Circle hoàn toàn mới", generic["content_prompt"])
        self.assertTrue(generic["common_prompt"])
        self.assertTrue(generic["selection_prompt"])

    def test_circle_prompt_override_has_priority_over_external_profile(self):
        resolved = prompt_profiles.resolve_circle_prompt({
            "circle_name": "Tin Tức 24h",
            "prompt_profile_id": "tin_tuc_24h",
            "content_prompt_override": "Chỉ chọn tin khoa học đã được xác minh rõ ràng.",
            "media_search_query_override": "tin khoa học Việt Nam",
        })
        self.assertEqual(resolved["prompt_source"], "circle_override")
        self.assertEqual(
            resolved["content_prompt"],
            "Chỉ chọn tin khoa học đã được xác minh rõ ràng.",
        )
        self.assertEqual(resolved["media_search_query"], "tin khoa học Việt Nam")

    def test_every_external_prompt_profile_has_content(self):
        profiles = prompt_profiles.load_prompt_profiles()
        self.assertTrue(profiles["common_prompt"])
        self.assertTrue(profiles["selection_prompt"])
        self.assertGreaterEqual(len(profiles["profiles"]), 8)
        for profile in profiles["profiles"]:
            self.assertTrue(profile["content_prompt"], profile["profile_id"])

    def test_writer_includes_circle_specific_prompt(self):
        prompt = build_writer_prompt(
            {
                "original_title": "Tin thử nghiệm",
                "original_description": "Mô tả",
                "source_name": "Nguồn",
                "source_url": "https://example.com",
            },
            {"topic": "thể thao", "key_facts": [], "overall_score": 90},
            {
                "circle_name": "Nét đẹp bóng đá",
                "common_prompt": "Không bịa dữ kiện và phải ghi nguồn rõ ràng.",
                "content_prompt": "Chỉ chọn tin bóng đá, không chọn môn thể thao khác.",
            },
        )
        self.assertIn("YÊU CẦU RIÊNG BẮT BUỘC", prompt)
        self.assertIn("QUY TẮC BIÊN TẬP CHUNG BẮT BUỘC", prompt)
        self.assertIn("Không bịa dữ kiện", prompt)
        self.assertIn("không chọn môn thể thao khác", prompt)

    def test_validator_accepts_attributed_source_image(self):
        post_data = {
            "ai_title": "Tiêu đề bài viết thử nghiệm hợp lệ",
            "ai_summary": " ".join(["nội dung"] * 60),
            "bullet_points": ["Ý chính thứ nhất", "Ý chính thứ hai"],
            "hashtags": ["#TinTuc", "#XaHoi", "#VietNam", "#TinMoi", "#CapNhat"],
            "source_url": "https://example.com/news/1",
            "source_name": "Nguồn thử nghiệm",
        }
        result = validate_post(post_data, post_data, image_meta={
            "file_path": "image.jpg",
            "status": "PASS",
            "is_ai_generated": False,
            "image_label": "🖼 Ảnh từ bài gốc — Nguồn: Nguồn thử nghiệm",
        })
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["score"], 100)

    def test_validator_rejects_post_without_image(self):
        post_data = {
            "ai_title": "Tiêu đề bài viết thử nghiệm hợp lệ",
            "ai_summary": " ".join(["nội dung"] * 60),
            "bullet_points": ["Ý chính thứ nhất", "Ý chính thứ hai"],
            "hashtags": ["#TinTuc", "#XaHoi", "#VietNam", "#TinMoi", "#CapNhat"],
            "source_url": "https://example.com/news/1",
            "source_name": "Nguồn thử nghiệm",
        }
        result = validate_post(post_data, post_data, image_meta={
            "file_path": "",
            "status": "PENDING",
            "is_ai_generated": True,
            "image_label": "🖼 Chưa có ảnh minh họa",
        })
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["passed"])
        self.assertIn("Thiếu ảnh hợp lệ", " ".join(result["reasons"]))

    @patch("src.publisher.save_publish_log")
    @patch("src.publisher.update_post_status")
    @patch("src.publisher._run_async")
    @patch("src.publisher.claim_post_for_publish", return_value=True)
    @patch("src.publisher.can_publish_now", return_value=(True, "OK"))
    @patch("src.publisher.get_post_by_id")
    @patch("src.publisher.logger")
    def test_publish_failure_restores_ready_for_retry(
        self,
        _logger_mock,
        get_post_mock,
        _can_publish_mock,
        _claim_mock,
        run_async_mock,
        update_status_mock,
        _save_log_mock,
    ):
        get_post_mock.return_value = {
            "id": 99,
            "status": "READY",
            "circle_id": "circle_tintuc24h",
            "full_content": "Nội dung bài đăng",
            "image_path": "",
        }

        def close_coroutine(coroutine):
            coroutine.close()
            return False, "", "Lỗi Playwright thử nghiệm"

        run_async_mock.side_effect = close_coroutine
        with patch("src.publisher._has_valid_image", return_value=True):
            self.assertFalse(publish_post(99, max_retries=1))
        _can_publish_mock.assert_called_once_with(
            "circle_tintuc24h", enforce_min_interval=True
        )
        update_status_mock.assert_called_with(99, "READY", reject_reason="Lỗi Playwright thử nghiệm")
        self.assertEqual(get_last_publish_error(99), "Lỗi Playwright thử nghiệm")

    @patch("src.queue_manager.get_last_successful_publish_at")
    @patch("src.queue_manager.count_published_this_hour", return_value=0)
    @patch("src.queue_manager.count_published_today", return_value=0)
    def test_manual_override_keeps_hourly_daily_limits_but_skips_interval(
        self,
        _count_today_mock,
        _count_hour_mock,
        last_publish_mock,
    ):
        last_publish_mock.return_value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        allowed_automatic, reason = can_publish_now(
            "circle_tintuc24h", enforce_min_interval=True
        )
        allowed_manual, _ = can_publish_now(
            "circle_tintuc24h", enforce_min_interval=False
        )

        self.assertFalse(allowed_automatic)
        self.assertIn("chờ thêm", reason)
        self.assertTrue(allowed_manual)

    @patch("src.publisher.save_publish_log")
    @patch("src.publisher.update_post_status")
    @patch("src.publisher._run_async")
    @patch("src.publisher.claim_post_for_publish", return_value=True)
    @patch("src.publisher.can_publish_now", return_value=(True, "OK"))
    @patch("src.publisher.get_post_by_id")
    def test_manual_publish_bypasses_only_min_interval(
        self,
        get_post_mock,
        can_publish_mock,
        _claim_mock,
        run_async_mock,
        _update_status_mock,
        _save_log_mock,
    ):
        get_post_mock.return_value = {
            "id": 100,
            "status": "READY",
            "circle_id": "circle_tintuc24h",
            "full_content": "Nội dung bài đăng",
            "image_path": "",
        }

        def succeed_without_browser(coroutine):
            coroutine.close()
            return True, "post-test", ""

        run_async_mock.side_effect = succeed_without_browser

        with patch("src.publisher._has_valid_image", return_value=True):
            self.assertTrue(publish_post(100, max_retries=1, manual_override=True))
        can_publish_mock.assert_called_once_with(
            "circle_tintuc24h", enforce_min_interval=False
        )

    def test_publish_is_blocked_when_circle_publish_checkbox_is_off(self):
        post = {
            "id": 101,
            "status": "READY",
            "circle_id": "circle_test_disabled",
            "full_content": "Nội dung",
            "image_path": "",
        }
        with patch("src.publisher.get_post_by_id", return_value=post), patch(
            "src.publisher._get_circle_config",
            return_value={
                "circle_id": "circle_test_disabled",
                "circle_name": "Circle thử nghiệm",
                "enabled": True,
                "publish_enabled": False,
            },
        ), patch("src.publisher._has_valid_image", return_value=True), patch(
            "src.publisher.claim_post_for_publish"
        ) as claim_mock:
            self.assertFalse(publish_post(101, max_retries=1, manual_override=True))
            claim_mock.assert_not_called()
            self.assertIn("Đăng bài", get_last_publish_error(101))

    def test_publish_is_blocked_and_waits_without_image(self):
        post = {
            "id": 102,
            "status": "READY",
            "circle_id": "circle_tintuc24h",
            "full_content": "Nội dung hợp lệ nhưng chưa có ảnh",
            "image_path": "",
        }
        with patch("src.publisher.get_post_by_id", return_value=post), patch(
            "src.publisher._has_valid_image", return_value=False
        ), patch("src.publisher.update_post_status") as update_mock, patch(
            "src.publisher.claim_post_for_publish"
        ) as claim_mock:
            self.assertFalse(publish_post(102, max_retries=1))
            claim_mock.assert_not_called()
            update_mock.assert_called_once_with(
                102,
                "WAITING_IMAGE",
                reject_reason="Bài #102 chưa có ảnh hợp lệ hoặc chưa được xác minh với nội dung hiện tại; hãy tiếp tục kiểm tra ảnh.",
            )


if __name__ == "__main__":
    unittest.main()
