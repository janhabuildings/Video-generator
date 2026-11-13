import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import motion
from engine.pipeline import Project, Scene, duration, execute, generate_audio, generate_image, run


class MotionTests(unittest.TestCase):
    def test_paid_calls_require_explicit_opt_in(self):
        with self.assertRaisesRegex(ValueError, "allow_paid_generation"):
            Project(script="A brick facade", provider="openai")
        with self.assertRaisesRegex(ValueError, "requires provider"):
            Project(script="A brick facade", animation="generated")

    def test_submit_once_poll_and_save_task_without_signed_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "reference.jpg"
            image.write_bytes(b"jpeg-fixture")
            responses = [{"id": "task-123"}, {"status": "RUNNING"},
                         {"status": "SUCCEEDED", "output": ["https://cdn.example/video.mp4?secret=signed"]}]
            with patch.object(motion, "request", side_effect=responses) as request, \
                 patch.object(motion, "download") as download:
                record = motion.generate(image, root / "video.mp4", "Slow dolly", root / "task.json",
                                         Mock(), poll_seconds=0)
            self.assertEqual(request.call_count, 3)
            self.assertEqual(request.call_args_list[0].args[0], "image_to_video")
            self.assertEqual(record["status"], "SUCCEEDED")
            self.assertNotIn("signed", (root / "task.json").read_text())
            download.assert_called_once()

    def test_failed_tasks_are_not_resubmitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "reference.jpg"
            image.write_bytes(b"fixture")
            with patch.object(motion, "request", side_effect=[{"id": "task"}, {"status": "FAILED"}]) as request:
                with self.assertRaisesRegex(motion.MotionError, "No automatic resubmission"):
                    motion.generate(image, root / "out.mp4", "Dolly", root / "task.json", Mock())
            self.assertEqual(request.call_count, 2)
            self.assertEqual(json.loads((root / "task.json").read_text())["status"], "FAILED")

    def test_timeout_retains_task_id_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root / "reference.jpg"
            image.write_bytes(b"fixture")
            with patch.object(motion, "request", return_value={"id": "task"}) as request:
                with self.assertRaisesRegex(motion.MotionError, "may still be running"):
                    motion.generate(image, root / "out.mp4", "Dolly", root / "task.json", Mock(), timeout_seconds=0)
            request.assert_called_once()
            self.assertEqual(json.loads((root / "task.json").read_text())["id"], "task")

    def test_generated_pipeline_renders_video_and_retains_longer_narration(self):
        demo = Project(script="Fixture")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def fake_image(project, scene, target, index):
                generate_image(demo, scene, target, index)

            def fake_audio(project, scene, target):
                generate_audio(demo, Scene(visual="Fixture", seconds=3), target)

            def fake_motion(image, target, prompt, task_file, progress):
                # Short real clip stands in for the external provider response.
                run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=320x180:r=24:d=1", "-an", "-c:v", "libx264", "-threads", "1", str(target))
                task_file.write_text('{"id":"fixture-task","status":"SUCCEEDED"}')
                return {"id": "fixture-task", "status": "SUCCEEDED"}

            project = Project(provider="openai", allow_paid_generation=True, animation="generated",
                              scenes=[Scene(visual="Brick facade", seconds=2)])
            with patch("engine.pipeline.generate_image", side_effect=fake_image), \
                 patch("engine.pipeline.generate_audio", side_effect=fake_audio), \
                 patch.object(motion, "generate", side_effect=fake_motion):
                manifest = execute(project, root, Mock())
            self.assertGreaterEqual(duration(root / "video.mp4"), 3)
            self.assertIn("motion-001.mp4", manifest["artifacts"])
            self.assertEqual(manifest["motion_tasks"][0]["id"], "fixture-task")
            probe = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(root / "video.mp4")))
            video = next(s for s in probe["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (1920, 1080))
            self.assertTrue(any(s["codec_type"] == "audio" for s in probe["streams"]))
