"""Runway image-to-video adapter. Submission is never automatically retried."""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class MotionError(RuntimeError):
    """A sanitized provider failure suitable for returning to API clients."""


def request(endpoint: str, payload=None):
    key = os.getenv("RUNWAYML_API_SECRET")
    if not key:
        raise MotionError("Set RUNWAYML_API_SECRET on the server for generated motion")
    req = urllib.request.Request("https://api.dev.runwayml.com/v1/" + endpoint,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "X-Runway-Version": "2024-11-06"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise MotionError(f"Motion provider returned HTTP {error.code}. No automatic resubmission was made.") from None
    except (urllib.error.URLError, TimeoutError):
        raise MotionError("Motion provider connection failed. A submitted task may still incur charges; check the provider before resubmitting.") from None


def download(url: str, target: Path):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise MotionError("Motion provider returned an invalid download URL")
    # No API authorization header is sent to the artifact host.
    temporary = target.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as output:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 256 * 1024 * 1024:
                    raise MotionError("Generated clip exceeds the 256 MB download limit")
                output.write(chunk)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def generate(image: Path, target: Path, prompt: str, task_file: Path, progress,
             *, poll_seconds=5, timeout_seconds=1200):
    # Normalize to JPEG beforehand in the pipeline to stay below the data-URI limit.
    data_uri = "data:image/jpeg;base64," + base64.b64encode(image.read_bytes()).decode()
    if len(data_uri) > 4_500_000:
        raise MotionError("Motion reference image exceeds the encoded upload limit")
    model = os.getenv("RUNWAY_VIDEO_MODEL", "seedance2_5")
    result = request("image_to_video", {"model": model, "promptImage": data_uri,
        "promptText": prompt, "ratio": "1280:720", "duration": 5})
    task_id = result.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise MotionError("Motion provider did not return a task ID")
    record = {"provider": "runway", "id": task_id, "model": model,
              "duration": 5, "status": "SUBMITTED"}
    task_file.write_text(json.dumps(record, indent=2))
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = request("tasks/" + urllib.parse.quote(task_id, safe=""))
        state = result.get("status", "UNKNOWN")
        record["status"] = state
        task_file.write_text(json.dumps(record, indent=2))
        progress(f"Motion task {state}")
        if state == "SUCCEEDED":
            outputs = result.get("output") or []
            if not outputs or not isinstance(outputs[0], str):
                raise MotionError("Motion task completed without a video output")
            download(outputs[0], target)
            return record
        if state in ("FAILED", "CANCELED", "CANCELLED"):
            raise MotionError("Motion task failed or was canceled. No automatic resubmission was made.")
        time.sleep(poll_seconds)
    raise MotionError("Motion polling timed out. The task may still be running and incur charges; check its saved ID before resubmitting.")
