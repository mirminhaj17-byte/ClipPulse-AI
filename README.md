# ClipPulse AI — free starter

A self-hosted browser app for recorded-video clipping and public YouTube/Twitch live capture.

## v0.3 preview features
- Automatic candidate highlight suggestions based on low-memory audio activity analysis.
- Ranked candidate moments with timestamps and one-tap clip creation.
- Analysis scans mono low-rate audio instead of loading the full video into memory.

The first-pass ranker is a transparent speech/activity heuristic, not a trained model and not a guarantee of virality. Semantic understanding, speaker tracking, animated captions, persistent storage and resilient background jobs are future upgrades.

## v0.2 features
- GitHub sign-in for each creator.
- Private creator dashboard and per-account clipping history.
- Upload MP4/MOV/MKV/WEBM/M4V files (up to 550 MB).
- Create a time-range clip and export 9:16, 16:9 or 1:1 MP4.
- Start a Streamlink capture for a public YouTube/Twitch URL.
- Mobile-friendly browser UI.

## Guest mode (default)
- No GitHub OAuth app or manual credentials are needed.
- Each browser receives a signed guest session and sees only its own clip history.
- Guest sessions are temporary and may reset after a service restart or redeploy.
- This starter is intended for personal testing. A public production service should add rate limits, abuse protection and durable storage before inviting unrestricted public use.

GitHub OAuth routes remain in the backend for a possible future account-based mode, but the default browser interface does not require them.

## Run locally
Requires Python 3.11+, FFmpeg and Streamlink.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000.

## Run with Docker
```bash
docker build -t clippulse-ai .
docker run --rm -p 8000:8000 -e SESSION_SECRET="replace-with-a-long-random-value" -v "$PWD/data:/app/data" clippulse-ai
```

## Free hosting limitations
The app uses SQLite for history in this starter. Render's free service filesystem is temporary, so the database and generated MP4 files can disappear after a restart or redeploy. For durable history and downloadable clips, connect a free managed database and object storage before relying on long-term retention. Free Render instances can also spin down when idle and have limited memory/CPU; large uploads may still fail during FFmpeg processing. Automatic highlight analysis is audio-only but can still take time on long videos.

## Important notes
- Use only content you own or are authorized to download/record. Platform terms and copyright law still apply.
- Live stream access can fail when a stream is private, restricted, geo-blocked, DRM-protected or unsupported by Streamlink.
- The live clip timestamp is approximate in this starter and depends on stream buffering.
