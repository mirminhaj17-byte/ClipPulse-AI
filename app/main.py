import os, re, uuid, subprocess, threading, time, sqlite3, secrets, json, shutil
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
def load_session_secret():
    configured = os.getenv("SESSION_SECRET")
    if configured:
        return configured
    secret_file = BASE / "session_secret.key"
    try:
        if secret_file.exists():
            value = secret_file.read_text().strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        secret_file.write_text(value)
        try:
            secret_file.chmod(0o600)
        except OSError:
            pass
        return value
    except OSError:
        return secrets.token_urlsafe(48)

app = FastAPI(title="ClipPulse AI", version="0.2.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=load_session_secret(),
    same_site="lax",
    https_only=os.getenv("RENDER", "") == "true",
    max_age=60 * 60 * 24 * 7,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
live_jobs = {}
lock = threading.Lock()
clip_semaphore = threading.Semaphore(1)
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

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
    # Per-browser guest identity; no OAuth setup is required.
    user = request.session.get("user")
    if not user:
        user = {
            "id": "guest-" + uuid.uuid4().hex,
            "login": "guest",
            "name": "Guest creator",
            "avatar_url": "",
        }
        request.session["user"] = user
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

@app.get("/api/ping")
def ping():
    # Lightweight browser heartbeat. On Render Free, periodic inbound requests
    # keep an actively used live-capture session from idling out.
    return {"ok": True, "time": int(time.time())}

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
    user = current_user(request)
    return {"authenticated": True, "user": user, "guest_mode": True}

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
                raise HTTPException(413, "Maximum upload size is 200 MB on the free instance.")
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
    job_id = uuid.uuid4().hex  # 128-bit bearer ID; remains valid if the guest cookie rotates
    target = LIVE / f"{job_id}.ts"
    try:
        handle = target.open("wb")
        proc = subprocess.Popen(["streamlink","--stdout",url,"best"], stdout=handle, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise HTTPException(500, "Streamlink is not installed.")
    owner_id = str(request.session["user"]["id"])
    with lock:
        live_jobs[job_id] = {"proc":proc,"handle":handle,"path":target,"started":time.time(),"url":url,"user_id":owner_id}
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS live_sessions (
            live_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, url TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("INSERT OR REPLACE INTO live_sessions(live_id,user_id,url) VALUES(?,?,?)",
                     (job_id, owner_id, url))
    return {"live_id":job_id,"status":"recording"}

class LiveClipRequest(BaseModel):
    live_id: str
    seconds_ago: int = 45
    duration: int = 30
    aspect: str = "9:16"

@app.get("/api/live/status/{live_id}")
def live_status(live_id: str, request: Request):
    user = current_user(request)
    with lock:
        job = live_jobs.get(live_id)
    # Render restarts clear the in-memory process registry. Resume from the
    # persisted source URL; live_id is a cryptographically random bearer token.
    if not job:
        with db() as conn:
            saved = conn.execute("SELECT url FROM live_sessions WHERE live_id=?", (live_id,)).fetchone()
        if saved:
            recovered_path = LIVE / f"{live_id}.ts"
            try:
                handle = recovered_path.open("wb")
                proc = subprocess.Popen(["streamlink", "--stdout", saved["url"], "best"],
                                         stdout=handle, stderr=subprocess.DEVNULL)
                job = {"proc": proc, "handle": handle, "path": recovered_path,
                       "started": time.time(), "url": saved["url"], "user_id": str(user["id"])}
                with lock:
                    live_jobs[live_id] = job
            except OSError:
                return {"recording": False, "reason": "Could not resume the stream. Start a new capture."}
        else:
            return {"recording": False, "reason": "The capture record is no longer available. Start a new capture."}
    proc = job["proc"]
    alive = proc.poll() is None
    size = job["path"].stat().st_size if job["path"].exists() else 0
    return {"recording": alive, "bytes": size,
            "reason": "" if alive else "The stream process stopped. Start a new capture."}

@app.post("/api/live/clip")
def clip_live(req: LiveClipRequest, request: Request):
    user = current_user(request)
    with lock:
        job = live_jobs.get(req.live_id)
    if not job:
        # A deploy/restart kills the in-memory Streamlink process. Recover the
        # saved source automatically, then ask the creator to let it buffer.
        with db() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS live_sessions (
                live_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, url TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            saved = conn.execute("SELECT url FROM live_sessions WHERE live_id=?",
                                 (req.live_id,)).fetchone()
        if not saved:
            raise HTTPException(404, "Live session expired. Start a new capture.")
        recovered_path = LIVE / f"{req.live_id}.ts"
        try:
            handle = recovered_path.open("wb")
            proc = subprocess.Popen(["streamlink","--stdout",saved["url"],"best"],
                                    stdout=handle, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise HTTPException(500, "Streamlink is not installed.")
        job = {"proc":proc,"handle":handle,"path":recovered_path,"started":time.time(),
               "url":saved["url"],"user_id":str(user["id"])}
        with lock:
            live_jobs[req.live_id] = job
        raise HTTPException(409, "The server restarted and live capture has been automatically resumed. Keep this page open and wait at least 70 seconds before clipping.")
    if not 1 <= req.duration <= 120 or not req.duration <= req.seconds_ago <= 3600:
        raise HTTPException(400, "Clip length must be 1–120 seconds, and the clip must end at least its own length ago.")
    if req.aspect not in ("9:16", "16:9", "1:1"):
        raise HTTPException(400, "Unsupported aspect ratio.")

    source = job["path"]
    proc = job["proc"]
    if proc.poll() is not None:
        raise HTTPException(409, "The live stream has stopped. Start a new capture.")
    if not source.exists() or source.stat().st_size < 188 * 10:
        raise HTTPException(409, "The live stream has not buffered enough video yet. Keep capture running for 30–60 seconds, then retry.")

    # Snapshot the growing transport stream so FFmpeg reads a stable file.
    snapshot = LIVE / f"snapshot_{uuid.uuid4().hex}.ts"
    outname = f"liveclip_{uuid.uuid4().hex}.mp4"
    out = OUTPUTS / outname
    try:
        shutil.copyfile(source, snapshot)
        # MPEG-TS consists of 188-byte packets; discard a partial final packet.
        size = snapshot.stat().st_size
        usable = size - (size % 188)
        if usable < 188 * 10:
            raise HTTPException(409, "The live recording is still buffering. Wait another 30 seconds and retry.")
        with snapshot.open("r+b") as stream:
            stream.truncate(usable)

        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(snapshot)],
            capture_output=True, text=True, timeout=20
        )
        try:
            buffered_seconds = float(probe.stdout.strip())
        except (TypeError, ValueError):
            buffered_seconds = 0.0
        if probe.returncode or buffered_seconds <= 0:
            raise HTTPException(422, "The live capture file is not a valid video stream. Stop this capture and start a fresh one.")

        # 'seconds_ago' means how far before the live edge the clip should END.
        if buffered_seconds < req.seconds_ago + req.duration:
            needed = int(req.seconds_ago + req.duration - buffered_seconds)
            raise HTTPException(409, f"Not enough recorded footage yet. Keep the stream running for about {needed} more seconds, then retry.")

        clip_start = buffered_seconds - req.seconds_ago - req.duration
        vf = {"9:16":"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
              "16:9":"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
              "1:1":"scale=720:720:force_original_aspect_ratio=increase,crop=720:720"}[req.aspect]
        if not clip_semaphore.acquire(blocking=False):
            raise HTTPException(429, "Another clip is being processed. Please wait and try again.")
        try:
            run_ffmpeg(["-threads","1","-filter_threads","1","-fflags","+discardcorrupt+genpts",
                        "-ss",str(clip_start),"-i",str(snapshot),"-t",str(req.duration),
                        "-vf",vf,"-c:v","libx264","-preset","ultrafast","-threads","1",
                        "-crf","28","-c:a","aac","-b:a","96k","-movflags","+faststart",str(out)])
        finally:
            clip_semaphore.release()
        save_history(user, "Live", job["url"], outname)
        return {"clip_id":outname,"url":f"/api/download/{outname}",
                "buffered_seconds":round(buffered_seconds,1),"start":round(clip_start,1)}
    except HTTPException:
        out.unlink(missing_ok=True)
        raise
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        raise HTTPException(408, "Checking the live recording took too long. Wait a little and retry.")
    except (OSError, RuntimeError) as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not create a clip from this live recording: {str(exc)[-500:]}")
    finally:
        snapshot.unlink(missing_ok=True)


@app.post("/api/live/stop/{live_id}")
def stop_live(live_id: str, request: Request):
    user = current_user(request)
    with lock:
        job = live_jobs.get(live_id)
        if not job:
            raise HTTPException(404, "Live recording not found.")
        live_jobs.pop(live_id, None)
    job["proc"].terminate()
    job["handle"].close()
    with db() as conn:
        conn.execute("DELETE FROM live_sessions WHERE live_id=? AND user_id=?",
                     (live_id, str(user["id"])))
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
