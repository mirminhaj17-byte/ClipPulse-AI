"""Low-memory highlight suggestions using audio activity and visual scene changes.

This is a transparent heuristic, not a trained semantic or virality model.
FFmpeg decodes streams incrementally; the source video is never loaded into RAM.
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
        duration = float(p.stdout.strip())
        if duration <= 0:
            raise ValueError
        return duration
    except (TypeError, ValueError):
        raise RuntimeError("The uploaded file does not contain a readable video duration.")


def _has_audio(path: Path) -> bool:
    p = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(path)
    ], timeout=30)
    return bool(p.stdout.strip())


def _audio_activity(path: Path, duration: float):
    if not _has_audio(path):
        return [(0.0, duration)], False
    p = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
        "-ac", "1", "-ar", "8000",
        "-af", "silencedetect=noise=-34dB:d=0.45", "-f", "null", "-"
    ], timeout=300)
    if p.returncode:
        raise RuntimeError("Audio analysis failed. Try an MP4 with a supported audio track.")
    silences, pending = [], None
    for line in (p.stderr or "").splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m:
            pending = float(m.group(1))
        m = re.search(r"silence_end:\s*([0-9.]+)", line)
        if m and pending is not None:
            silences.append((max(0.0, pending), min(duration, float(m.group(1)))))
            pending = None
    if pending is not None:
        silences.append((max(0.0, pending), duration))
    active, cursor = [], 0.0
    for start, end in silences:
        if start > cursor:
            active.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        active.append((cursor, duration))
    # Do not mistake a completely silent track for continuous activity.\n    return active, True


def _scene_changes(path: Path):
    """Return timestamps of visually distinct frames using FFmpeg's scene score."""
    p = _run([
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
        "-vf", "select='gt(scene,0.28)',showinfo", "-vsync", "vfr",
        "-f", "null", "-"
    ], timeout=300)
    if p.returncode:
        raise RuntimeError("Visual scene analysis failed. Check that FFmpeg can decode this video.")
    found = []
    for line in (p.stderr or "").splitlines():
        if "showinfo" not in line:
            continue
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
            if not found or t - found[-1] > 1.0:
                found.append(t)
    return found


def detect_highlights(path: Path, clip_seconds: int = 30, limit: int = 6):
    duration = probe_duration(path)
    if duration < 8:
        raise ValueError("Video is too short. Upload a video at least 8 seconds long.")

    speech, audio_available = _audio_activity(path, duration)
    scenes = _scene_changes(path)

    # Keep clips shorter than the source where possible. Very short sources
    # still return one full-length candidate rather than an invalid timestamp.
    window = min(max(12, int(clip_seconds)), 60, max(1, int(duration)))
    if window >= duration:
        starts = [0.0]
    else:
        step = max(4, window // 4)
        starts = list(range(0, max(1, int(duration - window) + 1), step))
        starts.append(max(0.0, duration - window))
        # Add candidates around scene changes, with a little lead-in.
        starts.extend(max(0.0, min(duration - window, t - 1.5)) for t in scenes)

    starts = sorted(set(round(s, 1) for s in starts))
    results = []
    for start in starts:
        end = min(duration, start + window)
        active = sum(max(0.0, min(end, b) - max(start, a)) for a, b in speech)
        coverage = min(1.0, active / max(1.0, end - start)) if audio_available else 0.0
        scene_count = sum(start + 1.0 <= t <= end - 1.0 for t in scenes)
        # Reward meaningful visual changes and active audio without treating
        # continuous background audio as proof that a moment is interesting.
        audio_points = coverage * 32 if audio_available else 0
        visual_points = min(scene_count, 5) * 7
        score = min(99, round(35 + audio_points + visual_points))
        if coverage >= 0.10 or scene_count:
            reason_parts = []
            if audio_available and coverage >= 0.55:
                reason_parts.append("active audio")
            elif audio_available and coverage >= 0.10:
                reason_parts.append("audio with pauses")
            if scene_count:
                reason_parts.append(f"{scene_count} visual scene change(s)")
            results.append({
                "start": round(start, 1),
                "duration": round(end - start, 1),
                "score": score,
                "reason": ", ".join(reason_parts) or "visual review candidate"
            })

    results.sort(key=lambda x: (x["score"], x["start"]), reverse=True)
    chosen = []
    for item in results:
        if all(abs(item["start"] - prior["start"]) >= window * 0.55 for prior in chosen):
            chosen.append(item)
        if len(chosen) >= limit:
            break
    return {
        "video_duration": round(duration, 1),
        "method": "audio activity + FFmpeg visual scene-change heuristic",
        "audio_available": audio_available,
        "scene_changes_detected": len(scenes),
        "items": chosen
    }
