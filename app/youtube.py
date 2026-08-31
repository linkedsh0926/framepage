from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .config import Settings


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DEMO_VIDEO_ID = "FramePageDemo"
ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


class InvalidYouTubeURL(ValueError):
    pass


class VideoInspectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YouTubeRef:
    video_id: str
    canonical_url: str


def parse_youtube_url(value: str, *, allow_demo: bool = False) -> YouTubeRef:
    try:
        parsed = urlsplit(value.strip())
        parsed_port = parsed.port
    except ValueError as exc:
        raise InvalidYouTubeURL("Malformed YouTube URL") from exc
    if parsed.scheme != "https":
        raise InvalidYouTubeURL("Only HTTPS YouTube URLs are accepted")
    if parsed.username or parsed.password or parsed_port:
        raise InvalidYouTubeURL("Credentials and custom ports are not accepted")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise InvalidYouTubeURL("Use a supported youtube.com or youtu.be URL")

    query = parse_qs(parsed.query, keep_blank_values=True)
    if "list" in query:
        raise InvalidYouTubeURL("Playlists are not supported")
    pieces = [piece for piece in parsed.path.split("/") if piece]
    video_id: str | None = None
    if host == "youtu.be":
        if len(pieces) != 1:
            raise InvalidYouTubeURL("Invalid youtu.be video path")
        video_id = pieces[0]
    elif parsed.path.rstrip("/") == "/watch":
        values = query.get("v", [])
        if len(values) != 1:
            raise InvalidYouTubeURL("YouTube watch URL must contain one video id")
        video_id = values[0]
    elif len(pieces) == 2 and pieces[0] in {"shorts", "embed"}:
        video_id = pieces[1]
    else:
        raise InvalidYouTubeURL("Unsupported YouTube URL form")

    valid_demo = allow_demo and video_id == DEMO_VIDEO_ID
    if not VIDEO_ID_RE.fullmatch(video_id or "") and not valid_demo:
        raise InvalidYouTubeURL("Invalid YouTube video id")
    canonical_query = urlencode({"v": video_id})
    canonical = urlunsplit(("https", "www.youtube.com", "/watch", canonical_query, ""))
    return YouTubeRef(video_id=video_id, canonical_url=canonical)


def _estimate(duration_seconds: int) -> dict[str, int]:
    baseline = max(3, round(duration_seconds / 75))
    return {
        "min_pages": max(2, round(baseline * 0.75)),
        "max_pages": max(4, round(baseline * 1.35)),
        "estimated_minutes": max(1, round(duration_seconds / 240)),
    }


def inspect_video(value: str, settings: Settings) -> dict:
    ref = parse_youtube_url(value, allow_demo=settings.mode in {"demo", "test"})
    if settings.mode in {"demo", "test"}:
        duration = 12 * 60 + 34
        return {
            "video": {
                "video_id": ref.video_id,
                "canonical_url": ref.canonical_url,
                "title": "FramePage 演示：渐进式课件与关键帧",
                "duration_seconds": duration,
                "thumbnail_url": f"https://i.ytimg.com/vi/{ref.video_id}/hqdefault.jpg",
                "channel": "FramePage Demo",
            },
            "estimate": _estimate(duration),
            "mode": settings.mode,
        }

    executable = shutil.which("yt-dlp")
    if not executable:
        raise VideoInspectionError("yt-dlp is unavailable on this server")
    proc = subprocess.run(
        [
            executable,
            "--dump-single-json",
            "--skip-download",
            "--no-playlist",
            "--no-warnings",
            ref.canonical_url,
        ],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if proc.returncode != 0:
        raise VideoInspectionError(
            "The video is unavailable. Use a public video that does not require login."
        )
    try:
        metadata = json.loads(proc.stdout)
        duration = int(metadata.get("duration") or 0)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise VideoInspectionError("YouTube returned invalid video metadata") from exc
    final_ref = parse_youtube_url(
        metadata.get("webpage_url") or ref.canonical_url, allow_demo=False
    )
    if final_ref.video_id != ref.video_id:
        raise VideoInspectionError("YouTube redirected to a different video")
    if duration <= 0 or duration > settings.max_video_seconds:
        raise VideoInspectionError("Video duration must be between 1 second and 120 minutes")
    return {
        "video": {
            "video_id": ref.video_id,
            "canonical_url": ref.canonical_url,
            "title": str(metadata.get("title") or "Untitled video")[:500],
            "duration_seconds": duration,
            "thumbnail_url": metadata.get("thumbnail"),
            "channel": metadata.get("channel") or metadata.get("uploader"),
        },
        "estimate": _estimate(duration),
        "mode": "real",
    }
