"""Low-memory, audio-activity highlight suggestions.

This is a deterministic first-pass ranker, not a trained virality predictor.
It scans only a mono low-bitrate audio stream so large source videos are not
loaded into RAM. Candidate moments are ranked by speech/activity coverage and
the number of speech transitions.
"""
import re
import subprocess
from pathlib import Path


def _run(args, timeout=240):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def probe_duration(path: Path) -> float:
    p = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], timeout=30)
    if p.returncode:
        raise RuntimeError("Could not read video duration. Check that the file is a valid video.")
    try:
        return float(p.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError("The uploaded file does not contain a readable video duration.")


def detect_highlights(path: Path, clip_seconds: int = 30, limit: int = 6):
    duration = probe_duration(path)
    if duration < 8:
        raise ValueError("Video is too short. Upload a video at least 8 seconds long.")

    # Audio-only decode avoids loading the full video into memory.
    p = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-ac", "1", "-ar", "8000", "-af", "silencedetect=noise=-34dB:d=0.45",
        "-f", "null", "-"
    ], timeout=300)
    log = (p.stderr or "")
    if p.returncode and ("does not contain any stream" not in log.lower()):
        if "matches no streams" in log.lower() or "does not contain any stream" in log.lower():
            speech = [(0.0, duration)]
        else:
            raise RuntimeError("Audio analysis failed. Try an MP4 with an audio track.")
    else:
        silences = []
        pending = None
        for line in log.splitlines():
            m = re.search(r"silence_start:\s*([0-9.]+)", line)
            if m:
                pending = float(m.group(1))
            m = re.search(r"silence_end:\s*([0-9.]+)", line)
            if m and pending is not None:
                silences.append((max(0.0, pending), min(duration, float(m.group(1)))))
                pending = None
        if pending is not None:
            silences.append((max(0.0, pending), duration))

        speech = []
        cursor = 0.0
        for start, end in silences:
            if start > cursor:
                speech.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < duration:
            speech.append((cursor, duration))
        if not speech:
            speech = [(0.0, duration)]

    window = min(max(12, int(clip_seconds)), 60, int(duration))
    if window >= duration:
        starts = [0.0]
    else:
        step = max(5, window // 3)
        starts = list(range(0, max(1, int(duration - window) + 1), step))
        final_start = max(0, duration - window)
        if not starts or final_start - starts[-1] > 3:
            starts.append(final_start)

    results = []
    for start in starts:
        end = min(duration, start + window)
        active = 0.0
        transitions = 0
        for a, b in speech:
            overlap = max(0.0, min(end, b) - max(start, a))
            active += overlap
            if overlap > 0.4 and (a >= start + 1 or b <= end - 1):
                transitions += 1
        coverage = min(1.0, active / max(1.0, end - start))
        # A transparent heuristic: prefer sustained speech with some changes.
        score = min(99, round(48 + coverage * 43 + min(transitions, 4) * 2))
        if coverage >= 0.18:
            results.append({
                "start": round(start, 1),
                "duration": round(end - start, 1),
                "score": score,
                "reason": "Strong speech/activity" if coverage >= 0.65 else "Speech with pauses"
            })

    results.sort(key=lambda x: (x["score"], x["start"]), reverse=True)
    chosen = []
    for item in results:
        if all(abs(item["start"] - prior["start"]) >= window * 0.65 for prior in chosen):
            chosen.append(item)
        if len(chosen) >= limit:
            break
    return {"video_duration": round(duration, 1), "method": "audio activity heuristic", "items": chosen}
