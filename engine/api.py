"""Single-instance API with durable job records and one render worker."""
import hmac
import json
import logging
import os
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .pipeline import Project, execute, plan

DATA = Path(os.getenv("ENGINE_DATA_DIR", "data")).resolve()
LOG = logging.getLogger(__name__)
gate = Lock()


def database():
    connection = sqlite3.connect(DATA / "jobs.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def update(job_id, **values):
    with database() as connection:
        connection.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in values)
                           + " WHERE id=?", [*values.values(), job_id])


def work(job_id: str, project: Project):
    try:
        update(job_id, status="running", stage="Starting")
        manifest = execute(project, DATA / job_id,
            lambda stage, progress: update(job_id, stage=stage, progress=progress))
        update(job_id, status="completed", stage="Completed", progress=1,
               artifacts=json.dumps(manifest["artifacts"]))
    except Exception as error:
        LOG.error("Job %s failed (%s)", job_id, type(error).__name__)
        # Only predictable user/provider errors are exposed; no secrets or paths.
        message = str(error) if isinstance(error, ValueError) else "Generation failed; check provider access, quota, and renderer installation."
        update(job_id, status="failed", stage="Failed", error=message)


@asynccontextmanager
async def lifespan(app):
    DATA.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
            progress REAL DEFAULT 0, request TEXT NOT NULL, artifacts TEXT DEFAULT '[]',
            error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        connection.execute("""UPDATE jobs SET status='failed', stage='Interrupted',
            error='Server restarted; submit a new job. No automatic paid retries.'
            WHERE status IN ('queued','running')""")
    app.state.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="renderer")
    yield
    app.state.worker.shutdown(wait=True)


app = FastAPI(title="JBI Architectural Media Engine", version="0.1.0", lifespan=lifespan,
    description="Script to architectural images, narration and animated HD video. "
                "Demo outputs use geometric placeholders and silence. Generated narration is AI speech.")
bearer = HTTPBearer(auto_error=False)


def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    key = os.getenv("ENGINE_API_KEY", "")
    if not key:
        raise HTTPException(503, "Set ENGINE_API_KEY on the server before using the API")
    if not credentials or not hmac.compare_digest(credentials.credentials.encode(), key.encode()):
        raise HTTPException(401, "Invalid API token", headers={"WWW-Authenticate": "Bearer"})


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/v1/storyboards", dependencies=[Depends(authorize)])
def storyboard(project: Project):
    try:
        scenes = plan(project)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return {"scenes": [scene.model_dump() for scene in scenes],
            "estimated_seconds": sum(scene.seconds for scene in scenes),
            "note": "Timing expands if narration is longer. Plain script paragraphs become scenes; "
                    "use explicit scenes to separate narration from visual directions."}


@app.post("/v1/jobs", status_code=202, dependencies=[Depends(authorize)])
def create_job(project: Project):
    try:
        plan(project)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if project.provider == "openai" and not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OpenAI provider has not been configured on the server")
    with gate:
        with database() as connection:
            pending = connection.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if pending >= 20:
                raise HTTPException(429, "Render queue is full", headers={"Retry-After": "60"})
            job_id = uuid.uuid4().hex
            connection.execute("INSERT INTO jobs (id,status,stage,request) VALUES (?,?,?,?)",
                               (job_id, "queued", "Queued", project.model_dump_json()))
        app.state.worker.submit(work, job_id, project)
    return {"id": job_id, "status": "queued", "status_url": f"/v1/jobs/{job_id}",
            "demo": project.provider == "demo"}


def get_record(job_id):
    with database() as connection:
        record = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not record:
        raise HTTPException(404, "Job not found")
    return dict(record)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorize)])
def get_job(job_id: str):
    record = get_record(job_id)
    record.pop("request")
    record["artifacts"] = [{"name": name, "url": f"/v1/jobs/{job_id}/artifacts/{name}"}
                           for name in json.loads(record["artifacts"])]
    return record


@app.get("/v1/jobs/{job_id}/artifacts/{name}", dependencies=[Depends(authorize)])
def artifact(job_id: str, name: str):
    record = get_record(job_id)
    if record["status"] != "completed" or name not in json.loads(record["artifacts"]):
        raise HTTPException(404, "Artifact not available")
    return FileResponse(DATA / record["id"] / name, filename=name)
