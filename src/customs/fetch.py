"""Fetch a commercial from a YouTube link into an upload folder.

The console's second way in: instead of uploading the master, the user pastes
the YouTube URL of their commercial and the crew clears that. This module is
deliberately narrow:

* Only YouTube. The app is public and downloads whatever this module accepts,
  so the URL is parsed and reduced to an 11-character video id or refused;
  nothing else on the internet can be made to fetch (no SSRF surface), and
  the id is rebuilt into a canonical watch URL rather than passing the user's
  string to the downloader.
* The same limits as an upload, enforced before bandwidth is spent: the
  video's metadata is read first and a too-long video is refused without
  downloading a byte. The download itself is capped at 720p (plenty for the
  analyst's keyframes) and at the byte limit, and the result is re-checked on
  disk. create_run then re-probes duration with ffprobe exactly as it does
  for an upload, so a lying metadata field changes nothing.

yt-dlp is imported lazily inside fetch_youtube: the console imports this
module on every boot, tests inject a fake downloader class, and the real
dependency is only paid for when a link is actually fetched.

Honest limit, stated where the code lives: YouTube rate-limits and sometimes
challenges datacenter IPs ("confirm you are not a bot"). From a laptop this
works; from Cloud Run it can be refused for popular videos. The refusal
surfaces as the 400 below with YouTube's own words in it, never as a hang.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
          "music.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}


class FetchError(Exception):
    """A refusal with a sentence the console can 400 verbatim."""


def youtube_id(url: str) -> str | None:
    """The 11-character video id, or None for anything that is not YouTube.

    Accepts the shapes people actually paste: watch?v=, youtu.be/, /shorts/,
    /embed/ and /live/. Everything else, other hosts included, is None.
    """
    try:
        parts = urlparse((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    if host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    elif host in _HOSTS:
        if parts.path == "/watch":
            candidate = (parse_qs(parts.query).get("v") or [""])[0]
        elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
            pieces = parts.path.split("/")
            candidate = pieces[2] if len(pieces) > 2 else ""
        else:
            return None
    else:
        return None
    return candidate if _ID.match(candidate or "") else None


def fetch_youtube(url: str, dest_dir, max_seconds: float, max_bytes: int,
                  ydl_cls=None) -> Path:
    """Download one YouTube video into dest_dir and return the file's path.

    Raises FetchError with a user-facing sentence for every refusal: not a
    YouTube link, a live stream, too long, too large, or YouTube saying no.
    ydl_cls is the downloader class (tests inject a fake; production uses
    yt_dlp.YoutubeDL).
    """
    video_id = youtube_id(url)
    if video_id is None:
        raise FetchError(
            "Only YouTube links are accepted: youtube.com/watch?v=..., "
            "youtu.be/... or a Shorts link.")
    if ydl_cls is None:
        from yt_dlp import YoutubeDL as ydl_cls  # lazy: see module docstring

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    canonical = f"https://www.youtube.com/watch?v={video_id}"

    # Which player clients to ask for depends on whether we hold cookies.
    # Without them, the web client is the one YouTube bot-checks hardest on
    # datacenter IPs (Cloud Run hit "Sign in to confirm you're not a bot" on
    # day one), so tv and android go first: they are challenged far less.
    # WITH cookies the order flips: account cookies are exactly what
    # satisfies the web client's check, while the tv client with cookies
    # attached answers "The page needs to be reloaded" (seen live on both the
    # laptop and Cloud Run). YT_COOKIES_FILE is the operator escape hatch: a
    # Netscape-format export mounted as a secret, never committed.
    quiet = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "noplaylist": True,
    }
    cookies = os.environ.get("YT_COOKIES_FILE", "").strip()
    jar = None
    if cookies and Path(cookies).is_file():
        # yt-dlp rewrites the cookie file on close, and a secret mounted into
        # the container is read-only, so it gets a scratch copy. Cleaned up
        # before the fetched file is returned; the folder is this upload's own.
        jar = dest / ".cookies.txt"
        jar.write_bytes(Path(cookies).read_bytes())
        quiet["cookiefile"] = str(jar)
        quiet["extractor_args"] = {"youtube": {"player_client": ["web", "web_safari"]}}
    else:
        quiet["extractor_args"] = {"youtube": {"player_client": ["tv", "android", "web"]}}

    try:
        with ydl_cls({**quiet, "skip_download": True}) as ydl:
            info = ydl.extract_info(canonical, download=False) or {}
    except Exception as exc:  # noqa: BLE001 -- YouTube's refusal becomes the 400
        if "Sign in to confirm" in str(exc):
            raise FetchError(
                "YouTube challenged this server with a bot check and refused "
                "the video. Try again in a minute, or upload the file "
                "directly; it clears identically.") from exc
        raise FetchError(f"YouTube would not hand over that video: {exc}") from exc

    if info.get("is_live"):
        raise FetchError("That link is a live stream; Customs clears finished "
                         "commercials.")
    duration = info.get("duration") or 0
    if not duration:
        raise FetchError("YouTube reported no duration for that video, so the "
                         "120 second limit cannot be checked. Upload the file "
                         "instead.")
    if duration > max_seconds:
        raise FetchError(
            f"That video is {duration:.0f} seconds long. Customs clears "
            f"commercials up to {int(max_seconds)} seconds.")

    # The file keeps the video's own title as its stem because telemetry
    # labels every metric and alert with the asset path's stem: the dashboards
    # should say spring_launch, not dQw4w9WgXcQ.
    stem = _SAFE.sub("_", info.get("title") or video_id).strip("_")[:60] or video_id
    try:
        with ydl_cls({
            **quiet,
            "outtmpl": str(dest / f"{stem}.%(ext)s"),
            "format": "bv*[height<=720]+ba/b[height<=720]/b",
            "merge_output_format": "mp4",
            "max_filesize": max_bytes,
        }) as ydl:
            ydl.download([canonical])
    except Exception as exc:  # noqa: BLE001
        raise FetchError(f"The download failed: {exc}") from exc

    if jar is not None:
        jar.unlink(missing_ok=True)
    files = [p for p in sorted(dest.glob(f"{stem}.*"))
             if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    if not files:
        # yt-dlp skips a too-large file silently; no file is how we hear it.
        raise FetchError(
            f"That video is over the {max_bytes // (1024 * 1024)} MB limit, "
            "or the download produced nothing.")
    path = files[0]
    if path.stat().st_size > max_bytes:
        path.unlink(missing_ok=True)
        raise FetchError(
            f"That video is over the {max_bytes // (1024 * 1024)} MB limit.")
    return path
