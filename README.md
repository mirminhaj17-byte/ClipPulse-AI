# ClipPulse AI — free starter

A self-hosted browser app for recorded-video clipping and public YouTube/Twitch live capture.

## v0.2 features
- GitHub sign-in for each creator.
- Private creator dashboard and per-account clipping history.
- Upload MP4/MOV/MKV/WEBM/M4V files (up to 550 MB).
- Create a time-range clip and export 9:16, 16:9 or 1:1 MP4.
- Start a Streamlink capture for a public YouTube/Twitch URL.
- Mobile-friendly browser UI.

## GitHub login setup (required before sign-in works)
1. In GitHub, open **Settings → Developer settings → OAuth Apps → New OAuth App**.
2. Set **Application name** to `ClipPulse AI`.
3. Set **Homepage URL** to `https://clippulse-ai.onrender.com`.
4. Set **Authorization callback URL** to `https://clippulse-ai.onrender.com/auth/github/callback`.
5. Register the app and copy its Client ID. Generate a Client Secret.
6. In Render, open the **ClipPulse-AI** web service → **Environment** and add:
   - `GITHUB_CLIENT_ID` = the OAuth app Client ID
   - `GITHUB_CLIENT_SECRET` = the OAuth app Client Secret
   - `SESSION_SECRET` = a long random secret (at least 32 random characters)
7. Save changes and let Render redeploy.

Do not put the Client Secret or SESSION_SECRET in GitHub files, screenshots, or chat messages. Add them only to Render's private environment settings. The app requests only the `read:user` GitHub scope and does not need repository access.

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
The app uses SQLite for history in this starter. Render's free service filesystem is temporary, so the database and generated MP4 files can disappear after a restart or redeploy. For durable history and downloadable clips, connect a free managed database and object storage before relying on long-term retention. Free Render instances can also spin down when idle and have limited memory/CPU; large uploads may still fail during FFmpeg processing.

## Important notes
- Use only content you own or are authorized to download/record. Platform terms and copyright law still apply.
- Live stream access can fail when a stream is private, restricted, geo-blocked, DRM-protected or unsupported by Streamlink.
- The live clip timestamp is approximate in this starter and depends on stream buffering.
