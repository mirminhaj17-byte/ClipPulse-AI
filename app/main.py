import os, re, uuid, subprocess, threading, time, sqlite3, secrets, json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request as URLRequest, urlopen
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from app.highlights import detect_highlights

BASE = Path(os.getenv("CLIPPULSE_DATA", "./data")).resolve()
UPLOADS, OUTPUTS, LIVE = BASE / "uploads", BASE / "clips", BASE / "live"
for folder in (UPLOADS, OUTPUTS, LIVE):
    folder.mkdir(parents=True, exist_ok=True)
DB_PATH = BASE / "clippulse.sqlite3"
app = FastAPI(title="ClipPulse AI", version="0.2.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET") or secrets.token_urlsafe(48),
    same_site="lax",
    https_only=os.getenv("RENDER", "") == "true",
    max_age=60 * 60 * 24 * 7,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
live_jobs = {}
lock = threading.Lock()
clip_semaphore = threading.Semaphore(1)
MAX_UPLOAD_BYTES = 550 * 1024 * 1024

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_user ON clips(user_id, id DESC)")
init_db()

def current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "Please sign in with GitHub first.")
    return user

def save_history(user, kind, source_name, filename):
    with db() as conn:
        conn.execute(
            "INSERT INTO clips(user_id,username,kind,source_name,filename,status) VALUES(?,?,?,?,?,'completed')",
            (str(user["id"]), user["login"], kind, source_name, filename),
        )

def run_ffmpeg(args):
    proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-1800:] or "FFmpeg failed")

def safe_stream_url(url: str):
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    allowed = ("youtube.com", "youtu.be", "twitch.tv", "m.twitch.tv")
    if not url.startswith("https://") or not any(host == d or host.endswith("." + d) for d in allowed):
        raise HTTPException(400, "Only HTTPS YouTube and Twitch URLs are accepted.")
    return url

@app.get("/")
def home():
    return FileResponse("app/static/index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "name": "ClipPulse AI", "version": "0.2.0"}

@app.get("/auth/github")
def github_login(request: Request):
    client_id = os.getenv("GITHUB_CLIENT_ID")
    if not client_id:
        raise HTTPException(503, "GitHub login is not configured yet. Add GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in Render.")
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    callback = str(request.base_url).rstrip("/") + "/auth/github/callback"
    params = urlencode({"client_id": client_id, "redirect_uri": callback, "scope": "read:user", "state": state})
    return RedirectResponse("https://github.com/login/oauth/authorize?" + params)

@app.get("/auth/github/callback")
def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse("/?login=cancelled")
    expected = request.session.pop("oauth_state", None)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(400, "GitHub sign-in state check failed. Please try again.")
    client_id, client_secret = os.getenv("GITHUB_CLIENT_ID"), os.getenv("GITHUB_CLIENT_SECRET")
    if not client_id or not client_secret or not code:
        raise HTTPException(503, "GitHub OAuth credentials are missing.")
    callback = str(request.base_url).rstrip("/") + "/auth/github/callback"
    body = urlencode({"client_id": client_id, "client_secret": client_secret, "code": code, "redirect_uri": callback}).encode()
    token_req = URLRequest("https://github.com/login/oauth/access_token", data=body,
                           headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                           method="POST")
    try:
        with urlopen(token_req, timeout=15) as response:
            token_data = json.loads(response.read().decode())
        token = token_data.get("access_token")
        if not token:
            raise ValueError("GitHub did not return an access token.")
        user_req = URLRequest("https://api.github.com/user",
                              headers={"Authorization": "Bearer " + token,
                                       "Accept": "application/vnd.github+json",
                                       "User-Agent": "ClipPulse-AI"})
        with urlopen(user_req, timeout=15) as response:
            profile = json.loads(response.read().decode())
    except Exception:
        raise HTTPException(502, "Could not complete GitHub sign-in. Please try again.")
    request.session.clear()
    request.session["user"] = {
        "id": profile["id"], "login": profile["login"],
        "name": profile.get("name") or profile["login"],
        "avatar_url": profile.get("avatar_url", "")
    }
    return RedirectResponse("/")

@app.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}

@app.get("/api/me")
def me(request: Request):
    user = request.session.get("user")
    return {"authenticated": bool(user), "user": user}

@app.get("/api/history")
def history(request: Request):
    user = current_user(request)
    with db() as conn:
        rows = conn.execute(
            "SELECT id,kind,source_name,filename,status,created_at FROM clips WHERE user_id=? ORDER BY id DESC LIMIT 100",
            (str(user["id"]),)
        ).fetchall()
    return {"items": [dict(row) | {"url": f"/api/download/{row['filename']}"} for row in rows]}

@app.post("/api/upload")
async def upload_video(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "Upload MP4, MOV, MKV, WEBM or M4V video.")
    name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOADS / name
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Maximum upload size is 550 MB on the free instance.")
            out.write(chunk)
    # Keep ownership of the source upload in a separate temporary table.
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS uploads (
            video_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_name TEXT NOT NULL
        )""")
        conn.execute("INSERT INTO uploads(video_id,user_id,source_name) VALUES(?,?,?)",
                     (name, str(user["id"]), file.filename or name))
    return {"video_id": name, "filename": file.filename, "size": size}

@app.post("/api/auto-highlights")
def auto_highlights(request: Request, video_id: str, clip_seconds: int = 30):
    user = current_user(request)
    if Path(video_id).name != video_id:
        raise HTTPException(400, "Invalid video ID.")
    with db() as conn:
        upload = conn.execute(
            "SELECT source_name FROM uploads WHERE video_id=? AND user_id=?",
            (video_id, str(user["id"]))
        ).fetchone()
    if not upload:
        raise HTTPException(404, "Uploaded video not found in your account.")
    src = UPLOADS / video_id
    if not src.exists():
        raise HTTPException(404, "Upload has expired from temporary free storage. Please upload it again.")
    if not 12 <= clip_seconds <= 60:
        raise HTTPException(400, "Choose a clip length between 12 and 60 seconds.")
    if not clip_semaphore.acquire(blocking=False):
        raise HTTPException(429, "Another video analysis or clip is running. Please wait and try again.")
    try:
        try:
            result = detect_highlights(src, clip_seconds=clip_seconds, limit=6)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except subprocess.TimeoutExpired:
            raise HTTPException(408, "Analysis took too long on the free server. Try a shorter or smaller video.")
        except RuntimeError as exc:
            raise HTTPException(422, str(exc))
        return {"source_name": upload["source_name"], **result}
    finally:
        clip_semaphore.release()


class ClipRequest(BaseModel):
    video_id: str
    start: float = 0
    duration: float = 30
    aspect: str = "9:16"

@app.post("/api/clip")
def create_clip(req: ClipRequest, request: Request):
    user = current_user(request)
    if Path(req.video_id).name != req.video_id:
        raise HTTPException(400, "Invalid video ID.")
    with db() as conn:
        upload = conn.execute("SELECT source_name FROM uploads WHERE video_id=? AND user_id=?",
                              (req.video_id, str(user["id"]))).fetchone()
    if not upload:
        raise HTTPException(404, "Uploaded video not found in your account.")
    src = UPLOADS / req.video_id
    if not src.exists():
        raise HTTPException(404, "Uploaded video has expired from temporary free storage. Please upload it again.")
    if req.start < 0 or req.duration <= 0 or req.duration > 180:
        raise HTTPException(400, "Use a non-negative start and duration up to 180 seconds.")
    if req.aspect not in ("9:16", "16:9", "1:1"):
        raise HTTPException(400, "Unsupported aspect ratio.")
    if not clip_semaphore.acquire(blocking=False):
        raise HTTPException(429, "Another clip is being processed. Please wait and try again.")
    outname = f"clip_{uuid.uuid4().hex}.mp4"
    out = OUTPUTS / outname
    vf = {"9:16":"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
          "1:1":"scale=720:720:force_original_aspect_ratio=increase,crop=720:720",
          "16:9":"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720"}[req.aspect]
    try:
        run_ffmpeg(["-threads","1","-filter_threads","1","-ss",str(req.start),"-i",str(src),
                    "-t",str(req.duration),"-vf",vf,"-c:v","libx264","-preset","ultrafast",
                    "-threads","1","-crf","28","-c:a","aac","-b:a","96k","-movflags","+faststart",str(out)])
    except RuntimeError as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(500, f"Clip creation failed: {exc}")
    finally:
        clip_semaphore.release()
    save_history(user, "Recorded", upload["source_name"], outname)
    return {"clip_id": outname, "url": f"/api/download/{outname}"}

class LiveRequest(BaseModel):
    url: str

@app.post("/api/live/start")
def start_live(req: LiveRequest, request: Request):
    current_user(request)
    url = safe_stream_url(req.url)
    job_id = uuid.uuid4().hex[:10]
    target = LIVE / f"{job_id}.ts"
    try:
        handle = target.open("wb")
        proc = subprocess.Popen(["streamlink","--stdout",url,"best"], stdout=handle, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(500, "Streamlink is not installed.")
    with lock:
        live_jobs[job_id] = {"proc":proc,"handle":handle,"path":target,"started":time.time(),"url":url,"user_id":str(request.session["user"]["id"])}
    return {"live_id":job_id,"status":"recording"}

class LiveClipRequest(BaseModel):
    live_id: str
    seconds_ago: int = 45
    duration: int = 30
    aspect: str = "9:16"

@app.post("/api/live/clip")
def clip_live(req: LiveClipRequest, request: Request):
    user = current_user(request)
    with lock:
        job = live_jobs.get(req.live_id)
    if not job or job.get("user_id") != str(user["id"]):
        raise HTTPException(404, "Live recording not found.")
    if not 1 <= req.duration <= 120 or not req.duration <= req.seconds_ago <= 3600:
        raise HTTPException(400, "Choose a clip up to 120 seconds, ending no more than 60 minutes ago.")
    if req.aspect not in ("9:16","16:9","1:1"):
        raise HTTPException(400, "Unsupported aspect ratio.")
    elapsed = time.time() - job["started"]
    start = max(0, elapsed - req.seconds_ago)
    if not clip_semaphore.acquire(blocking=False):
        raise HTTPException(429, "Another clip is being processed. Please wait and try again.")
    outname = f"liveclip_{uuid.uuid4().hex}.mp4"
    out = OUTPUTS / outname
    vf = {"9:16":"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
          "16:9":"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
          "1:1":"scale=720:720:force_original_aspect_ratio=increase,crop=720:720"}[req.aspect]
    try:
        run_ffmpeg(["-threads","1","-filter_threads","1","-ss",str(start),"-i",str(job["path"]),
                    "-t",str(req.duration),"-vf",vf,"-c:v","libx264","-preset","ultrafast",
                    "-threads","1","-crf","28","-c:a","aac","-b:a","96k","-movflags","+faststart",str(out)])
    except RuntimeError as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(500, f"Live clip failed: {exc}")
    finally:
        clip_semaphore.release()
    save_history(user, "Live", job["url"], outname)
    return {"clip_id":outname,"url":f"/api/download/{outname}"}

@app.post("/api/live/stop/{live_id}")
def stop_live(live_id: str, request: Request):
    user = current_user(request)
    with lock:
        job = live_jobs.get(live_id)
        if not job or job.get("user_id") != str(user["id"]):
            raise HTTPException(404, "Live recording not found.")
        live_jobs.pop(live_id, None)
    job["proc"].terminate()
    job["handle"].close()
    return {"status":"stopped","recording":job["path"].name}

@app.get("/api/download/{clip_id}")
def download(clip_id: str, request: Request):
    user = current_user(request)
    if Path(clip_id).name != clip_id:
        raise HTTPException(400, "Invalid clip ID.")
    with db() as conn:
        row = conn.execute("SELECT 1 FROM clips WHERE filename=? AND user_id=?",
                           (clip_id, str(user["id"]))).fetchone()
    if not row:
        raise HTTPException(404, "Clip not found in your account.")
    path = OUTPUTS / clip_id
    if not path.exists():
        raise HTTPException(404, "Clip file is no longer on free temporary storage.")
    return FileResponse(path, media_type="video/mp4", filename=clip_id)
