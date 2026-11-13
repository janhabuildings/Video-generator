import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from engine.pipeline import Project, Scene, duration, plan, run


class PlanningTests(unittest.TestCase):
    def test_paragraphs_preserve_narration(self):
        scenes = plan(Project(script="Brick exterior.\n\nA bright courtyard."))
        self.assertEqual([s.narration for s in scenes], ["Brick exterior.", "A bright courtyard."])

    def test_empty_and_excessive_scripts_rejected(self):
        with self.assertRaises(ValueError):
            Project(script="  ")
        with self.assertRaises(ValueError):
            plan(Project(script="\n\n".join(["Scene"] * 25)))


class ApiTests(unittest.TestCase):
    def test_demo_job_renders_real_hd_media_and_protects_artifacts(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "ENGINE_DATA_DIR": folder, "ENGINE_API_KEY": "test-token"}):
            from engine import api
            importlib.reload(api)
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(api.app) as client:
                self.assertEqual(client.post("/v1/jobs", json={"script": "Facade"}).status_code, 401)
                self.assertEqual(client.post("/v1/jobs", headers=headers, json={}).status_code, 422)
                payload = {"scenes": [{"visual": "Facade", "seconds": 2, "camera": "pan_right"},
                                      {"visual": "Courtyard", "seconds": 2, "camera": "pull_out"}]}
                response = client.post("/v1/jobs", headers=headers, json=payload)
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["id"]
                for _ in range(120):
                    job = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
                    if job["status"] in ("completed", "failed"):
                        break
                    time.sleep(0.5)
                self.assertEqual(job["status"], "completed", job)
                video = Path(folder) / job_id / "video.mp4"
                probe = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video)))
                stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
                self.assertEqual((stream["width"], stream["height"]), (1920, 1080))
                self.assertTrue(any(s["codec_type"] == "audio" for s in probe["streams"]))
                self.assertGreaterEqual(duration(video), 4)
                self.assertEqual(client.get(f"/v1/jobs/{job_id}/artifacts/video.mp4", headers=headers).status_code, 200)
                self.assertEqual(client.get(f"/v1/jobs/{job_id}/artifacts/concat.txt", headers=headers).status_code, 404)
                self.assertEqual(client.get(f"/v1/jobs/{job_id}/artifacts/video.mp4").status_code, 401)


if __name__ == "__main__":
    unittest.main()
