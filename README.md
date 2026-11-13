# JBI Architectural Media Engine

An API for turning architectural scripts into images, narration, and animated video.
Supports camera movement over images and opt-in Runway image-to-video generation.
It does not reconstruct geometry-based 3D walkthroughs. Supports 1080p and 4K MP4 exports at 30 fps;
4K is an upscaled export, not native 4K source generation.

## Run locally

Requires Python 3.11+ and FFmpeg/ffprobe with libx264 support.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
# Set a strong secret in your environment (do not commit it).
export ENGINE_API_KEY='replace-with-your-private-api-token'
uvicorn engine.api:app --host 127.0.0.1 --port 8000
```

Interactive API documentation: `http://127.0.0.1:8000/docs`. Use its Authorize
button with the API token. `GET /health` is public; all media endpoints require
`Authorization: Bearer <ENGINE_API_KEY>`. Bind publicly only behind HTTPS.

## Generate a video

```sh
curl http://127.0.0.1:8000/v1/jobs \
  -H "Authorization: Bearer $ENGINE_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/building.json
```

The response returns a job ID and status URL. Poll that URL with the same
Authorization header until `completed` or `failed`. Completed jobs list protected
download URLs for each PNG, WAV, the final MP4, and a JSON manifest.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/storyboards` | Preview scene breakdown without generation or charges |
| `POST /v1/jobs` | Queue a media job; returns HTTP 202 |
| `GET /v1/jobs/{id}` | Status, progress, errors, and artifact URLs |
| `GET /v1/jobs/{id}/artifacts/{name}` | Download a completed artifact |

Set `output` to `images`, `audio`, or `video`. A plain `script` is split at blank
lines into scenes. For architectural use, explicit `scenes` are recommended:
separate `visual` directions from spoken `narration`, choose a minimum `seconds`
duration, and select `push_in`, `pull_out`, `pan_left`, or `pan_right` camera motion.
Video scenes extend to fit the complete narration. Audio-only jobs return per-scene
WAV files. Images-only jobs return per-scene PNG files. No background music is generated.

The shared `design_brief` describes materials, building identity, lighting, and
context. Styles: `photorealistic`, `watercolor`, `clay`, `blueprint`.
Independent image generation cannot guarantee geometric or material consistency
between views. Outputs are design concepts, not verified representations of plans.

## Generation providers

The default `provider: demo` requires no provider credentials and makes no external
requests. It renders geometric test images and silent WAV files. These are test
fixtures, not AI architectural imagery or speech.

For actual image and narration generation, set `OPENAI_API_KEY` on the server and
submit `provider: openai` and `allow_paid_generation: true`. Requests without this
explicit opt-in are rejected before any paid calls. These jobs incur provider charges. No live paid generation
was used to validate this initial implementation. Model access depends on your account.

| Environment variable | Default |
| --- | --- |
| `ENGINE_API_KEY` | Required for protected endpoints |
| `ENGINE_DATA_DIR` | `data` |
| `OPENAI_API_KEY` | Required for OpenAI jobs |
| `OPENAI_IMAGE_MODEL` | `gpt-image-2.5-sunburst` |
| `OPENAI_SPEECH_MODEL` | `gpt-4o-mini-tts` |
| `RUNWAYML_API_SECRET` | Required for generated animation |
| `RUNWAY_VIDEO_MODEL` | `seedance2_5` |

### Generated architectural motion

Set `animation: generated` with `provider: openai`, `output: video`, and
`allow_paid_generation: true`. Both OpenAI and Runway credentials must be configured
on the server. Images and narration use OpenAI; motion uses Runway. A scene's optional
`motion_prompt` overrides the default architectural camera direction.

The adapter follows the [Runway image-to-video guide](https://docs.dev.runwayml.com/guides/using-the-api/).
It submits one five-second 1280×720 clip per scene, polls the task, downloads the
result, and normalizes it into the requested 1080p or 4K export. These larger exports
are upscaled. Generated source clips and task metadata are included with completed
job downloads. Longer narration extends the final frame rather than generating
additional paid clips; a shorter requested scene trims the generated clip.
Original provider audio, if any, is replaced by the selected narration.

Task IDs are saved in each job directory before polling. Failed and timed-out tasks
are never automatically resubmitted. A timeout, download failure, or interrupted
connection does not cancel a remote task; it may still incur charges. Inspect saved
task IDs in the provider dashboard before submitting another job. Signed download
URLs are not persisted. Live provider access and visual quality still require a
credentialed smoke test. Generative motion can alter building details and is not
evidence of dimensional or construction accuracy.

Provider contracts: [image generation](https://developers.openai.com/api/reference/resources/images/methods/generate)
and [speech generation](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create).
The Speech API model is currently supported but scheduled for retirement on
January 6, 2027. A replacement Realtime speech adapter is needed before that date;
changing only the model name will not migrate to the Realtime API.
Generated narration must be identified as AI speech in consuming applications.

## Operations and limits

This is a single-instance prototype: run one Uvicorn worker. SQLite preserves job
records, and a single background thread serializes renders to control local resource
use. Up to 20 queued/running jobs are accepted. Requests allow at most 24 scenes,
with minimum durations between 2 and 60 seconds per scene (narration may extend them).
Output directories retain scripts and generated media; set storage permissions and
retention to suit your organization. Keep `data/`, credentials, and `.env` out of Git.

On restart, incomplete jobs are marked failed, without automatic paid retries.
Repeated job submissions are independent and can incur duplicate charges. Graceful
shutdown waits for accepted jobs. Before production, add a durable external worker
queue, idempotency keys, per-user access and quotas, storage retention, cancellation,
reference-image controls, and provider task recovery/cancellation.
The API token grants access to all jobs on the instance; this is not a multi-tenant service.

## Verification

```sh
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Tests exercise script planning, validation, authentication, artifact restrictions,
and an actual two-scene FFmpeg render, probing the resulting 1920×1080 video and
audio tracks. Additional tests simulate provider success, failure, and timeout and
exercise generated-clip assembly with a real local video fixture, including narration
longer than the source clip. Provider-backed generation requires a separate credentialed smoke test.
