import os, re, uuid, subprocess, threading, time
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

BASE = Path(os.getenv("CLIPPULSE_DATA", "./data")).resolve()
UPLOADS = BASE / "uploads"
OUTPUTS = BASE / "clips"
LIVE = BASE / "live"
for p in (UPLOADS, OUTPUTS, LIVE): p.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipPulse AI", version="0.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
live_jobs = {}
lock = threading.Lock()

def run_ffmpeg(args):
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-1800:] or "FFmpeg failed")
    return proc

def safe_stream_url(url: str):
    # MVP intentionally limits remote ingest to public YouTube and Twitch URLs.
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    allowed = ("youtube.com", "youtu.be", "twitch.tv", "m.twitch.tv")
    if not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(400, "For safety, live ingest currently accepts YouTube and Twitch URLs only.")
    return url

@app.get("/")
def home():
    return FileResponse("app/static/index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "name": "ClipPulse AI", "version": "0.1.0"}

@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "Upload MP4, MOV, MKV, WEBM or M4V video.")
    name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS / name
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > 2 * 1024**3:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Maximum upload size is 2 GB.")
            out.write(chunk)
    return {"video_id": name, "filename": file.filename, "size": size}

class ClipRequest(BaseModel):
    video_id: str
    start: float = 0
    duration: float = 30
    aspect: str = "9:16"

@app.post("/api/clip")
def create_clip(req: ClipRequest):
    if Path(req.video_id).name != req.video_id:
        raise HTTPException(400, "Invalid video ID.")
    src = UPLOADS / req.video_id
    if not src.exists(): raise HTTPException(404, "Uploaded video not found.")
    if req.start < 0 or req.duration <= 0 or req.duration > 180:
        raise HTTPException(400, "Use a non-negative start and duration up to 180 seconds.")
    if req.aspect not in ("9:16", "16:9", "1:1"):
        raise HTTPException(400, "Unsupported aspect ratio.")
    outname = f"clip_{uuid.uuid4().hex}.mp4"
    out = OUTPUTS / outname
    if req.aspect == "9:16":
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    elif req.aspect == "1:1":
        vf = "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080"
    else:
        vf = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080"
    try:
        run_ffmpeg(["-ss", str(req.start), "-i", str(src), "-t", str(req.duration),
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-c:a", "aac", "-movflags", "+faststart", str(out)])
    except RuntimeError as e:
        raise HTTPException(500, f"Clip creation failed: {e}")
    return {"clip_id": outname, "url": f"/api/download/{outname}"}

class LiveRequest(BaseModel):
    url: str

@app.post("/api/live/start")
def start_live(req: LiveRequest):
    url = safe_stream_url(req.url)
    job_id = uuid.uuid4().hex[:10]
    target = LIVE / f"{job_id}.ts"
    try:
        # Streamlink writes a transport stream that can be read as it grows.
        proc = subprocess.Popen(["streamlink", "--stdout", url, "best"],
                                stdout=target.open("wb"), stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(500, "Streamlink is not installed. Install the requirements listed in README.")
    with lock:
        live_jobs[job_id] = {"proc": proc, "path": target, "started": time.time(), "url": url}
    return {"live_id": job_id, "status": "recording"}

class LiveClipRequest(BaseModel):
    live_id: str
    seconds_ago: int = 45
    duration: int = 30
    aspect: str = "9:16"

@app.post("/api/live/clip")
def clip_live(req: LiveClipRequest):
    with lock: job = live_jobs.get(req.live_id)
    if not job: raise HTTPException(404, "Live recording not found.")
    if not 1 <= req.duration <= 120 or not req.duration <= req.seconds_ago <= 3600:
        raise HTTPException(400, "Choose a clip up to 120 seconds, ending no more than 60 minutes ago.")
    if req.aspect not in ("9:16", "16:9", "1:1"): raise HTTPException(400, "Unsupported aspect ratio.")
    outname = f"liveclip_{uuid.uuid4().hex}.mp4"
    out = OUTPUTS / outname
    vf = {"9:16":"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
          "16:9":"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
          "1:1":"scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080"}[req.aspect]
    # Approximate offset from current live edge; input is a growing MPEG-TS file.
    elapsed = time.time() - job["started"]
    start = max(0, elapsed - req.seconds_ago)
    try:
        run_ffmpeg(["-ss", str(start), "-i", str(job["path"]), "-t", str(req.duration),
                    "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", "-movflags", "+faststart", str(out)])
    except RuntimeError as e:
        raise HTTPException(500, f"Live clip failed. The stream may not have buffered enough: {e}")
    return {"clip_id": outname, "url": f"/api/download/{outname}"}

@app.post("/api/live/stop/{live_id}")
def stop_live(live_id: str):
    with lock: job = live_jobs.pop(live_id, None)
    if not job: raise HTTPException(404, "Live recording not found.")
    job["proc"].terminate()
    return {"status": "stopped", "recording": job["path"].name}

@app.get("/api/download/{clip_id}")
def download(clip_id: str):
    if Path(clip_id).name != clip_id: raise HTTPException(400, "Invalid clip ID.")
    path = OUTPUTS / clip_id
    if not path.exists(): raise HTTPException(404, "Clip not found.")
    return FileResponse(path, media_type="video/mp4", filename=clip_id)
