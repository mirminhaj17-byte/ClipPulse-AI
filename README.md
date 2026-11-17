# ClipPulse AI — free starter

A self-hosted browser app for basic recorded-video clipping and public YouTube/Twitch live capture.

## Included in v0.1
- Upload MP4/MOV/MKV/WEBM/M4V files (up to 2 GB).
- Create a time-range clip and export 9:16, 16:9 or 1:1 MP4.
- Start a Streamlink capture for a public YouTube/Twitch URL.
- Export a recent segment from the growing live recording.
- Mobile-friendly browser UI.

## Not included yet
- Automatic AI highlight selection, speaker/face tracking, animated captions, virality scoring, user accounts, cloud deployment and multi-user job queues. These are planned stages, not currently working features.

## Run locally
Requires Python 3.11+, FFmpeg and Streamlink.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
# Install FFmpeg and Streamlink separately, then:
streamlink --version
ffmpeg -version
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open http://localhost:8000.

## Run with Docker
```bash
docker build -t clippulse-ai .
docker run --rm -p 8000:8000 -v "$PWD/data:/app/data" clippulse-ai
```

## iPhone access
The app needs to run on a computer or hosted server. Once deployed, open its HTTPS address in Safari. An iPhone alone cannot host the always-on live capture worker reliably.

## Important notes
- Use only content you own or are authorized to download/record. Platform terms and copyright law still apply.
- Live stream access can fail when a stream is private, restricted, geo-blocked, DRM-protected or unsupported by Streamlink.
- The live clip timestamp is approximate in this starter and depends on the stream/container buffering.
- Keep the app behind authentication before exposing it to the public internet. Do not deploy this starter as an unrestricted public service.
