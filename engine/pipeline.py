"""Architectural script-to-media pipeline. No provider calls in demo mode."""
import base64
import json
import math
import os
import subprocess
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from . import motion


class Scene(BaseModel):
    visual: str = Field(min_length=1, max_length=6000)
    narration: str = Field(default="", max_length=4000)
    seconds: float = Field(default=5, ge=2, le=60)
    camera: Literal["push_in", "pull_out", "pan_left", "pan_right"] = "push_in"
    motion_prompt: str = Field(default="", max_length=800)


class Project(BaseModel):
    title: str = Field(default="Architectural visualization", max_length=200)
    script: str = Field(default="", max_length=24000)
    scenes: list[Scene] = Field(default_factory=list, max_length=24)
    design_brief: str = Field(default="Contemporary architectural visualization", max_length=4000)
    style: Literal["photorealistic", "watercolor", "clay", "blueprint"] = "photorealistic"
    resolution: Literal["1080p", "4k"] = "1080p"
    output: Literal["video", "images", "audio"] = "video"
    provider: Literal["demo", "openai"] = "demo"
    animation: Literal["camera", "generated"] = "camera"
    allow_paid_generation: bool = False
    voice: Literal["alloy", "ash", "coral", "sage", "verse"] = "alloy"

    @model_validator(mode="after")
    def require_script(self):
        if not self.scenes and not self.script.strip():
            raise ValueError("Supply a script or at least one scene")
        if self.provider != "demo" and not self.allow_paid_generation:
            raise ValueError("Set allow_paid_generation=true to explicitly enable paid provider calls")
        if self.animation == "generated" and (self.provider == "demo" or self.output != "video"):
            raise ValueError("Generated animation requires provider=openai and output=video")
        return self


def plan(project: Project) -> list[Scene]:
    if project.scenes:
        return project.scenes
    # Blank lines deliberately define scene boundaries, preserving all narration.
    paragraphs = [part.strip() for part in project.script.split("\n\n") if part.strip()]
    if len(paragraphs) > 24:
        raise ValueError("Maximum 24 scenes; combine script paragraphs")
    scenes = []
    for paragraph in paragraphs:
        if len(paragraph) > 4000:
            raise ValueError("Each script paragraph must be at most 4000 characters")
        scenes.append(Scene(visual=paragraph, narration=paragraph,
                            seconds=min(60, max(3, len(paragraph.split()) / 2.3))))
    return scenes


def run(*args: str):
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
    return result.stdout


def duration(path: Path) -> float:
    return float(run("ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)))


def api_request(endpoint: str, payload: dict) -> bytes:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OPENAI_API_KEY is required for the openai provider")
    request = urllib.request.Request("https://api.openai.com/v1/" + endpoint,
        data=json.dumps(payload).encode(), headers={"Authorization": f"Bearer {key}",
                                                   "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        # Do not persist provider response bodies or request credentials in job errors.
        raise RuntimeError(f"Generation provider returned HTTP {error.code}; check model access and quota") from None


def generate_image(project: Project, scene: Scene, path: Path, index: int):
    if project.provider == "demo":
        # Clearly synthetic fixture for verifying media processing; no AI claims.
        run("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"color=c=0x142536:s=1536x864,drawgrid=w=96:h=96:t=1:c=0x35546b,"
            f"drawbox=x={200 + index * 15}:y=180:w=600:h=500:color=0x678fa3:t=fill,"
            "drawbox=x=850:y=330:w=350:h=350:color=0xb49a72:t=fill",
            "-frames:v", "1", "-threads", "1", str(path))
        return
    prompt = (f"Architectural concept illustration. Shared design brief: {project.design_brief}. "
              f"Style: {project.style}. Scene: {scene.visual}. "
              "Keep material palette and building identity consistent with the brief. "
              "Compose for a landscape 16:9 frame; no text or watermarks.")
    result = json.loads(api_request("images/generations", {
        "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst"),
        "prompt": prompt, "size": "1536x1024", "quality": "high", "n": 1,
        "output_format": "png"}))
    path.write_bytes(base64.b64decode(result["data"][0]["b64_json"], validate=True))


def generate_audio(project: Project, scene: Scene, path: Path):
    if project.provider == "demo" or not scene.narration.strip():
        # Silence is an explicit fixture, not a synthesized voice.
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24000)
            audio.writeframes(b"\0\0" * round(scene.seconds * 24000))
        return
    path.write_bytes(api_request("audio/speech", {
        "model": os.getenv("OPENAI_SPEECH_MODEL", "gpt-4o-mini-tts"),
        "voice": project.voice, "input": scene.narration, "response_format": "wav"}))


def render_scene(image: Path, audio: Path, target: Path, scene: Scene, resolution: str):
    width, height = (3840, 2160) if resolution == "4k" else (1920, 1080)
    # Extend scenes to retain complete narration, rounding up to a whole video frame.
    frames = math.ceil(max(scene.seconds, duration(audio)) * 30)
    zoom = f"1+0.08*on/{max(1, frames-1)}"
    x, y = "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"
    if scene.camera == "pull_out":
        zoom = f"1.08-0.08*on/{max(1, frames-1)}"
    elif scene.camera in ("pan_left", "pan_right"):
        zoom = "1.08"
        progress = f"on/{max(1, frames-1)}"
        x = f"(iw-iw/zoom)*({progress})" if scene.camera == "pan_right" else f"(iw-iw/zoom)*(1-{progress})"
    filters = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
               f"crop={width}:{height},"
               f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps=30")
    run("ffmpeg", "-v", "error", "-y", "-i", str(image), "-i", str(audio),
        "-vf", filters, "-af", "apad", "-t", str(frames/30),
        "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(target))


def render_motion(source: Path, audio: Path, target: Path, scene: Scene, resolution: str):
    width, height = (3840, 2160) if resolution == "4k" else (1920, 1080)
    seconds = math.ceil(max(scene.seconds, duration(audio)) * 30) / 30
    # A single five-second generation per scene. Freeze the last frame if needed;
    # never silently generate more paid clips to cover longer narration.
    filters = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
               f"crop={width}:{height},fps=30,tpad=stop_mode=clone:stop_duration={seconds}")
    run("ffmpeg", "-v", "error", "-y", "-i", str(source), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-vf", filters, "-af", "apad",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac", "-ar", "48000",
        "-ac", "2", "-movflags", "+faststart", str(target))


def execute(project: Project, directory: Path, progress):
    directory.mkdir(parents=True, exist_ok=True)
    scenes = plan(project)
    manifest = {"project": project.model_dump(), "scenes": [s.model_dump() for s in scenes],
                "demo": project.provider == "demo", "artifacts": [],
                "limitations": ["Concept visualization, not a geometry-verified 3D walkthrough",
                                "Independent images may differ in building details",
                                "HD/4K exports may upscale lower-resolution sources"],
                "motion_tasks": []}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    for index, scene in enumerate(scenes, 1):
        progress(f"Generating scene {index} of {len(scenes)}", (index-1)/len(scenes))
        image, audio = directory / f"scene-{index:03}.png", directory / f"scene-{index:03}.wav"
        if project.output in ("images", "video"):
            generate_image(project, scene, image, index)
            manifest["artifacts"].append(image.name)
        if project.output in ("audio", "video"):
            generate_audio(project, scene, audio)
            manifest["artifacts"].append(audio.name)
        if project.output == "video":
            target = directory / f"clip-{index:03}.mp4"
            if project.animation == "generated":
                reference = directory / f"reference-{index:03}.jpg"
                run("ffmpeg", "-v", "error", "-y", "-i", str(image),
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                    "-frames:v", "1", "-q:v", "3", "-threads", "1", str(reference))
                source = directory / f"motion-{index:03}.mp4"
                task_file = directory / f"motion-task-{index:03}.json"
                prompt = scene.motion_prompt or (
                    f"Slow architectural camera {scene.camera.replace('_', ' ')}. "
                    "Preserve the building form and materials. Subtle natural environmental motion.")
                record = motion.generate(reference, source, prompt, task_file,
                    lambda stage: progress(f"Scene {index}: {stage}", (index-1)/len(scenes)))
                manifest["motion_tasks"].append(record)
                manifest["artifacts"].extend([source.name, task_file.name])
                render_motion(source, audio, target, scene, project.resolution)
            else:
                render_scene(image, audio, target, scene, project.resolution)
    if project.output == "video":
        progress("Assembling final video", 0.95)
        # All paths in the concat file are engine-generated relative names.
        listing = directory / "concat.txt"
        listing.write_text("\n".join(f"file 'clip-{i:03}.mp4'" for i in range(1, len(scenes)+1)))
        run("ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
            "-c", "copy", "-movflags", "+faststart", str(directory / "video.mp4"))
        manifest["artifacts"].append("video.mp4")
    manifest["artifacts"].append("manifest.json")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
