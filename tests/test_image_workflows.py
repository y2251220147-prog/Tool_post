"""Kiểm thử ngữ cảnh đầy đủ, thẩm định ảnh và tiếp tục bài Chờ ảnh bằng DB tạm."""

import asyncio
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlparse

from PIL import Image

from src import gemini_client, image_generator, image_workflow, prompt_profiles, storage
from src.image_context import current_image_is_approved, image_context_hash
from src.writer import _build_full_content


class ImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch("src.storage.get_db_path", return_value=str(Path(self.temp.name) / "images.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        storage.init_database()
        self.circle = {
            "circle_id": "circle_tips", "circle_name": "Mẹo hay mỗi ngày",
            "ownership_verified": True, "scan_enabled": True, "publish_enabled": True,
            "content_prompt": "Chỉ chọn mẹo thực tế có các bước rõ ràng và an toàn.",
            "image_prompt": "Thấy rõ đầu vòi nước và thao tác làm sạch được bài mô tả.",
        }
        self.analysis = {"topic": "mẹo gia đình", "key_facts": ["Dùng bàn chải mềm làm sạch đầu vòi nước"],
                         "overall_score": 90, "is_sensitive": False, "publish": True}
        storage.upsert_source({
            "id": "source", "name": "Nguồn thử", "domain": "example.com", "rss_urls": [],
            "category": "life", "source_type": "rss", "trust_level": 90,
            "requires_attribution": True, "image_policy": "ALLOW_WITH_ATTRIBUTION", "language": "vi", "enabled": True,
        })
        article_id = storage.save_article({
            "source_id": "source", "source_name": "Nguồn thử", "source_url": "https://example.com/tips",
            "original_title": "Cách vệ sinh đầu vòi nước đúng cách", "original_description": "Mô tả nguồn thử.",
            "author": "Nguồn thử", "published_at": "2026-09-29", "category": "life",
            "content_hash": "tips", "source_image_url": "https://example.com/tips.jpg", "status": "PROCESSED",
        })
        story_id = storage.save_story({
            "topic": "mẹo gia đình", "main_title": "Vệ sinh vòi nước", "key_facts": self.analysis["key_facts"],
            "confidence_score": 90, "sensitivity_level": "low", "is_sensitive": False, "status": "ANALYZED",
        })
        self.post = {
            "story_id": story_id, "article_id": article_id, "circle_id": "circle_tips",
            "ai_title": "Cách vệ sinh đầu vòi nước an toàn trong gia đình",
            "ai_summary": "Thực hiện vệ sinh vòi nước bằng dụng cụ phù hợp. " * 25 + "Chi tiết cuối: chỉ dùng bàn chải mềm.",
            "bullet_points": ["Dùng bàn chải mềm theo hướng dẫn", "Làm sạch phần đầu vòi nước"],
            "hashtags": ["#VoiNuoc", "#LamSach", "#BanChaiMem", "#MeoGiaDinh", "#MeoHayMoiNgay"],
            "source_name": "Nguồn thử", "source_url": "https://example.com/tips",
            "relevance_score": 90, "trust_score": 90, "freshness_score": 90, "confidence_score": 90,
            "ai_reason": "Phù hợp", "status": "WAITING_IMAGE", "image_analysis": self.analysis,
        }
        self.post["full_content"] = _build_full_content(self.post, self.post["source_url"], self.post["source_name"])
        self.post_id = storage.save_post(self.post)
        self.path = Path(self.temp.name) / "test.jpg"
        Image.new("RGB", (640, 360), color=(20, 100, 170)).save(self.path)
        self.selected = {
            "file_path": str(self.path), "file_name": self.path.name, "source_url": "https://example.com/tips.jpg",
            "source_name": "Nguồn thử", "image_prompt": "", "is_ai_generated": 0,
            "image_label": "🖼 Ảnh từ bài gốc — Nguồn: Nguồn thử", "provider": "source", "model": "ảnh bài gốc",
        }

    def _vision(self, **updates):
        return {"subject_match": True, "action_match": True, "context_match": True,
                "contradictions": [], "visual_defects": [], "misleading_event": False,
                "relevance_score": 92, "passed": True, "reason": "Ảnh thể hiện đúng thao tác làm sạch đầu vòi.",
                **updates}

    def _check(self, status="PASS", reason="Đúng đầu vòi và bàn chải mềm"):
        return {"status": status, "reason": reason, "score": 92 if status == "PASS" else 30,
                "result": self._vision(passed=status == "PASS")}

    def _save_asset(self, status="PASS"):
        return storage.save_media_asset({
            **self.selected, "post_id": self.post_id, "type": "image", "sensitivity_note": "",
            "context_hash": image_context_hash(self.post, self.analysis, self.circle),
            "validation_status": status, "validation_reason": "Đúng thao tác", "relevance_score": 92,
        })

    def test_prompt_contains_full_article_tail_points_and_circle_rules(self):
        request = image_generator._build_image_prompt_request(self.post, self.analysis, self.circle)
        self.assertIn(self.post["ai_summary"], request)
        self.assertIn("Chi tiết cuối: chỉ dùng bàn chải mềm.", request)
        self.assertIn(self.post["bullet_points"][0], request)
        self.assertIn(self.circle["image_prompt"], request)
        self.assertIn(self.circle["content_prompt"], request)

    def test_pollinations_prompt_is_not_truncated_at_700_characters(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.content = self.path.read_bytes()
        prompt = "A detailed illustration. " * 65 + "TAIL_SOFT_BRUSH"
        with patch.object(image_generator.httpx, "Client", return_value=client), patch.object(
            image_generator, "IMAGES_DIR", Path(self.temp.name)
        ):
            image_generator._generate_pollinations_image(self.post_id, {"image_prompt": prompt}, 5)
        sent_url = client.get.call_args.args[0]
        self.assertTrue(unquote(urlparse(sent_url).path).endswith(prompt))

    def test_oversized_prompt_is_rejected_without_silent_truncation(self):
        with patch.object(image_generator, "generate_json", return_value={"image_prompt": "x" * 4001}):
            with self.assertRaises(ValueError):
                image_generator.generate_image_prompt(self.post, self.analysis, self.circle)

    def test_wrong_subject_is_rejected_even_when_model_says_passed(self):
        with patch.object(image_generator, "generate_json", return_value=self._vision(subject_match=False)) as call:
            result = image_generator.validate_image_content(self.selected, self.post, self.analysis, self.circle)
        self.assertEqual(result["status"], "REJECT")
        self.assertEqual(call.call_args.kwargs["image_path"], str(self.path))
        self.assertIn("Chi tiết cuối", call.call_args.args[0])

    def test_untrusted_or_incomplete_vision_response_never_passes(self):
        for response in [self._vision(passed="true"), self._vision(relevance_score="92"), {"passed": True}]:
            with self.subTest(response=response), patch.object(image_generator, "generate_json", return_value=response):
                self.assertEqual(image_generator.validate_image_content(
                    self.selected, self.post, self.analysis, self.circle)["status"], "PENDING")

    def test_source_image_needs_visual_validation_before_ready(self):
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "_download_source_image", return_value=self.selected
        ), patch.object(image_generator, "validate_image_content", return_value=self._check()) as check:
            result = image_workflow.complete_post_image(self.post_id)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "READY")
        check.assert_called_once()
        self.assertTrue(current_image_is_approved(storage.get_post_by_id(self.post_id), self.circle))

    def test_quota_failure_retains_post_and_image_then_resumes_without_generation(self):
        original_text = self.post["ai_summary"]
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "_download_source_image", return_value=self.selected
        ), patch.object(image_generator, "validate_image_content", return_value=self._check("PENDING", "Gemini hết lượt")):
            result = image_workflow.complete_post_image(self.post_id)
        self.assertEqual(result["status"], "WAITING_IMAGE")
        self.assertEqual(storage.get_post_by_id(self.post_id)["ai_summary"], original_text)
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "_download_source_image"
        ) as download, patch.object(image_generator, "generate_image_prompt") as create, patch.object(
            image_generator, "validate_image_content", return_value=self._check()
        ):
            resumed = image_workflow.complete_post_image(self.post_id)
        self.assertEqual(resumed["status"], "READY")
        download.assert_not_called()
        create.assert_not_called()

    def test_approved_image_is_cached_but_editing_article_invalidates_it(self):
        self._save_asset()
        with patch.object(image_generator, "validate_image_content") as vision:
            result = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis, circle=self.circle)
        self.assertTrue(result["cached"])
        vision.assert_not_called()
        conn = storage.get_connection()
        conn.execute("UPDATE generated_posts SET ai_summary = ai_summary || ' Chi tiết đã đổi.' WHERE id = ?", (self.post_id,))
        conn.commit()
        conn.close()
        self.assertFalse(current_image_is_approved(storage.get_post_by_id(self.post_id), self.circle))
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "validate_image_content", return_value=self._check()
        ) as vision:
            result = image_workflow.complete_post_image(self.post_id)
        self.assertEqual(result["status"], "READY")
        vision.assert_called_once()

    def test_only_two_ai_attempts_and_second_uses_rejection_feedback(self):
        ai_image = {**self.selected, "is_ai_generated": 1, "provider": "pollinations", "model": "default"}
        with patch.object(image_generator, "generate_image_prompt", return_value={"image_prompt": "Correct faucet"}) as prompt, patch.object(
            image_generator, "_generate_pollinations_image", return_value=ai_image
        ) as create, patch.object(image_generator, "validate_image_content", side_effect=[
            self._check("REJECT", "Chỉ có căn bếp, thiếu vòi nước"), self._check("REJECT", "Thiếu bàn chải")
        ]):
            result = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis,
                circle=self.circle, force_regenerate=True)
        self.assertEqual(result["status"], "REJECT")
        self.assertEqual(create.call_count, 2)
        self.assertIn("thiếu vòi nước", prompt.call_args_list[1].args[3])
        self.assertEqual(storage.get_all_media_assets_by_post(self.post_id), [])

    def test_stop_after_creation_saves_image_and_does_not_call_vision(self):
        with patch.object(image_generator, "generate_image_prompt", return_value={"image_prompt": "Correct faucet"}), patch.object(
            image_generator, "_generate_pollinations_image", return_value=self.selected
        ), patch.object(image_generator, "validate_image_content") as vision:
            result = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis,
                circle=self.circle, force_regenerate=True, control=MagicMock(side_effect=[True, True, True, False]))
        self.assertEqual(result["status"], "PENDING")
        self.assertEqual(result["file_path"], str(self.path))
        vision.assert_not_called()

    def test_gemini_generation_receives_whole_article_and_requires_explicit_enable(self):
        with patch.object(image_generator, "generate_image") as create:
            with self.assertRaises(ValueError):
                image_generator._generate_gemini_image(self.post_id, self.post, self.analysis, self.circle, {}, "")
            create.assert_not_called()
        with patch.object(image_generator, "generate_image", return_value={"image_bytes": self.path.read_bytes(), "model": "gemini-test-image"}) as create, patch.object(
            image_generator, "IMAGES_DIR", Path(self.temp.name)
        ):
            result = image_generator._generate_gemini_image(self.post_id, self.post, self.analysis, self.circle,
                {"gemini_generation_enabled": True, "gemini_model": "gemini-test-image"}, "Đổi chủ thể cho đúng bài")
        self.assertIn(self.post["ai_summary"], create.call_args.args[0])
        self.assertIn(self.circle["image_prompt"], create.call_args.args[0])
        self.assertEqual(result["provider"], "gemini")

    def test_gemini_vision_request_contains_image_bytes_and_text(self):
        client = MagicMock()
        client.models.generate_content.return_value.text = json.dumps(self._vision())
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch.object(
            gemini_client, "_create_client", return_value=client
        ), patch.object(gemini_client, "_model_candidates", return_value=["gemini-test"]), patch.object(
            gemini_client, "_circuit_error", return_value=None
        ):
            gemini_client.generate_json("Full article context", image_path=str(self.path))
        parts = client.models.generate_content.call_args.kwargs["contents"].parts
        self.assertEqual(parts[0].inline_data.data, self.path.read_bytes())
        self.assertEqual(parts[1].text, "Full article context")

    def test_parallel_image_claim_and_published_post_are_blocked(self):
        self.assertTrue(storage.claim_post_for_image(self.post_id))
        self.assertFalse(storage.claim_post_for_image(self.post_id))
        storage.release_post_image(self.post_id)
        storage.update_post_status(self.post_id, "PUBLISHED")
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_workflow, "generate_and_save_image"
        ) as create:
            result = image_workflow.complete_post_image(self.post_id)
        self.assertEqual(result["error_code"], "busy")
        create.assert_not_called()

    def test_pending_circle_resumes_image_without_collecting_sources_or_writer(self):
        from review_ui import app as dashboard

        with patch.object(dashboard, "get_circle_config", return_value=self.circle), patch.object(
            dashboard, "complete_post_image", return_value={"success": True, "post_id": self.post_id, "status": "READY"}
        ) as complete, patch("src.media_collector.collect_media_for_circle") as collect, patch("src.writer.write_post") as write:
            result = dashboard._scan_and_generate_for_circle("circle_tips")
        self.assertTrue(result["resumed_image"])
        collect.assert_not_called()
        write.assert_not_called()
        complete.assert_called_once()

    def test_waiting_image_visible_and_manual_ready_cannot_bypass_validation(self):
        from review_ui import app as dashboard
        from fastapi import HTTPException

        class Request:
            async def json(self):
                return {"status": "READY"}

        with patch.object(dashboard, "get_circle_config", return_value=self.circle):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(dashboard.change_status(self.post_id, Request()))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(storage.get_posts_by_status("WAITING_IMAGE")), 1)

    def test_external_image_prompt_and_circle_override_are_editable(self):
        circle = prompt_profiles.resolve_circle_prompt({"circle_name": "Mẹo hay mỗi ngày", "prompt_profile_id": "meo_hay_moi_ngay"})
        self.assertIn("thao tác", circle["image_prompt"])
        circle["image_prompt_override"] = "Chỉ minh họa phần dụng cụ được mô tả."
        self.assertEqual(prompt_profiles.resolve_circle_prompt(circle)["image_prompt"], circle["image_prompt_override"])

    def test_failed_provider_keeps_prompt_and_resumes_without_another_planning_call(self):
        with patch.object(image_generator, "generate_image_prompt", return_value={
            "image_prompt": "Show a faucet and soft brush", "prompt_request": "Ngữ cảnh toàn bài đã gửi"
        }), patch.object(image_generator, "_download_source_image", return_value=None), patch.object(
            image_generator, "_generate_pollinations_image", side_effect=RuntimeError("503")
        ):
            result = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis, circle=self.circle)
        self.assertEqual(result["status"], "PENDING")
        stored = storage.get_latest_image_asset(self.post_id)
        self.assertEqual(stored["prompt_request"], "Ngữ cảnh toàn bài đã gửi")
        with patch.object(image_generator, "generate_image_prompt") as plan, patch.object(
            image_generator, "_download_source_image", return_value=None
        ), patch.object(image_generator, "_generate_pollinations_image", return_value=self.selected) as create, patch.object(
            image_generator, "validate_image_content", return_value=self._check()
        ):
            resumed = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis, circle=self.circle)
        self.assertEqual(resumed["status"], "PASS")
        plan.assert_not_called()
        self.assertEqual(create.call_args.args[1]["image_prompt"], stored["image_prompt"])

    def test_rejected_source_is_not_downloaded_and_checked_again_with_same_context(self):
        self._save_asset("REJECT")
        with patch.object(image_generator, "_download_source_image") as download, patch.object(
            image_generator, "generate_image_prompt", return_value={"image_prompt": "Faucet and brush"}
        ), patch.object(image_generator, "_generate_pollinations_image", return_value=self.selected), patch.object(
            image_generator, "validate_image_content", return_value=self._check()
        ) as vision:
            result = image_generator.generate_and_save_image(self.post_id, self.post, self.analysis, circle=self.circle)
        self.assertEqual(result["status"], "PASS")
        download.assert_not_called()
        vision.assert_called_once()

    def test_gemini_native_uses_final_image_not_thought_preview(self):
        from google.genai import types

        client = MagicMock()
        client.models.generate_content.return_value.candidates = [SimpleNamespace(content=SimpleNamespace(parts=[
            types.Part(thought=True, inline_data=types.Blob(mime_type="image/jpeg", data=b"preview")),
            types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=self.path.read_bytes())),
        ]))]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}), patch.object(
            gemini_client, "_create_client", return_value=client
        ), patch.object(gemini_client, "_circuit_error", return_value=None):
            result = gemini_client.generate_image("Ngữ cảnh bài viết đầy đủ", "gemini-test-image")
        self.assertEqual(result["image_bytes"], self.path.read_bytes())
        self.assertEqual(client.models.generate_content.call_args.kwargs["config"].response_modalities, ["IMAGE"])

    def test_provider_settings_validate_and_preserve_unrelated_config(self):
        from fastapi.testclient import TestClient
        from review_ui import app as dashboard

        config = Path(self.temp.name) / "automation.json"
        config.write_text(json.dumps({"images": {"strategy": "source_first"}, "browser": {"publish_headless": True}}), encoding="utf-8")
        with patch.object(dashboard, "AUTOMATION_CONFIG_PATH", config), TestClient(dashboard.app) as client:
            self.assertEqual(client.post("/api/images/settings", json=[]).status_code, 422)
            self.assertEqual(client.post("/api/images/settings", json={"provider": "unknown"}).status_code, 422)
            result = client.post("/api/images/settings", json={
                "provider": "pollinations", "gemini_model": "gemini-3.1-flash-image", "gemini_generation_enabled": False
            })
            self.assertEqual(result.status_code, 200)
            self.assertFalse(client.get("/api/images/settings").json()["images"]["gemini_generation_enabled"])
        saved = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(saved["images"]["strategy"], "source_first")
        self.assertTrue(saved["browser"]["publish_headless"])

    def test_http_image_completion_overview_and_dashboard_javascript(self):
        from fastapi.testclient import TestClient
        from review_ui import app as dashboard

        config_dir = Path(self.temp.name) / "config"
        config_dir.mkdir()
        (config_dir / "circles.json").write_text(json.dumps({"circles": [self.circle]}), encoding="utf-8")
        with patch.object(dashboard, "ROOT_DIR", Path(self.temp.name)), patch.object(
            dashboard, "get_circle_config", return_value=self.circle
        ), patch.object(dashboard, "get_discovered_circles", return_value=[{
            **self.circle, "local_circle_id": self.circle["circle_id"], "category_name": "Mẹo gia đình"
        }]), patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "_download_source_image", return_value=self.selected
        ), patch.object(image_generator, "validate_image_content", return_value=self._check()), TestClient(dashboard.app) as client:
            self.assertEqual(client.post(f"/api/posts/{self.post_id}/image", json=[]).status_code, 422)
            self.assertEqual(client.post(f"/api/posts/{self.post_id}/image", json={"regenerate": "true"}).status_code, 422)
            response = client.post(f"/api/posts/{self.post_id}/image", json={"regenerate": False})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            overview = client.get(f"/api/posts/{self.post_id}/overview").json()
            self.assertTrue(overview["image_current"])
            self.assertEqual(overview["image"]["provider"], "source")
            rendered = client.get("/?tab=posts").text
        self.assertIn("Hướng dẫn ảnh riêng cho Circle", rendered)
        self.assertIn("Kiểm tra / tiếp tục ảnh", rendered)
        scripts = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", rendered, flags=re.S))
        checked = subprocess.run(["C:/Program Files/nodejs/node.exe", "--check"], input=scripts,
                                 text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_latest_pending_asset_prevents_using_an_older_passed_image(self):
        self._save_asset("PASS")
        self._save_asset("PENDING")
        post = storage.get_post_by_id(self.post_id)
        self.assertEqual(post["image_validation_status"], "PENDING")
        self.assertFalse(current_image_is_approved(post, self.circle))
        self.assertEqual(storage.get_all_media_assets_by_post(self.post_id), [])

    def test_changing_circle_image_rules_invalidates_old_approval(self):
        self._save_asset("PASS")
        changed = {**self.circle, "image_prompt": "Cần thấy đầy đủ thao tác khác."}
        self.assertFalse(current_image_is_approved(storage.get_post_by_id(self.post_id), changed))

    def test_backfill_source_requires_new_visual_check_not_previous_ai_approval(self):
        self._save_asset("PASS")
        with patch.object(image_workflow, "get_circle_config", return_value=self.circle), patch.object(
            image_generator, "_download_source_image", return_value=self.selected
        ) as download, patch.object(image_generator, "validate_image_content", return_value=self._check("PENDING", "Chưa xác minh ảnh mới")) as check, patch.object(
            image_generator, "generate_image_prompt"
        ) as create:
            result = image_workflow.complete_post_image(self.post_id, force_source=True)
        self.assertEqual(result["status"], "WAITING_IMAGE")
        self.assertFalse(current_image_is_approved(storage.get_post_by_id(self.post_id), self.circle))
        download.assert_called_once()
        check.assert_called_once()
        create.assert_not_called()

    def test_resume_recovers_a_dead_image_worker_without_waiting_fifteen_minutes(self):
        conn = storage.get_connection()
        conn.execute("UPDATE generated_posts SET image_work_state='PROCESSING', image_work_owner=999999 WHERE id=?",
                     (self.post_id,))
        conn.commit()
        conn.close()
        with patch.object(storage, "_image_worker_alive", return_value=False):
            self.assertTrue(storage.claim_post_for_image(self.post_id))
        storage.release_post_image(self.post_id)

    def test_startup_does_not_unlock_a_live_worker_even_when_paused_a_long_time(self):
        conn = storage.get_connection()
        conn.execute("""UPDATE generated_posts SET image_work_state='PROCESSING', image_work_owner=999999,
                        updated_at='2000-01-01' WHERE id=?""", (self.post_id,))
        conn.commit()
        conn.close()
        with patch.object(storage, "_image_worker_alive", return_value=True):
            storage.init_database()
            self.assertFalse(storage.claim_post_for_image(self.post_id))


if __name__ == "__main__":
    unittest.main()
