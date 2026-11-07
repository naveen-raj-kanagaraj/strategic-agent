"""Video downloading for the agent (ported from telegram-video-bot).

Two ways in:
  1. Fast path: the Telegram handler sees a bare link and calls download_video() directly.
  2. Agent tool: Claude calls the download_video tool; the file is queued in OUTBOX and the
     Telegram handler sends it after the reply.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

import requests
import yt_dlp

# Telegram's standard bot API rejects uploads over 50 MB.
MAX_MB = int(os.getenv("VIDEO_MAX_MB", "50"))
MAX_FILE_SIZE = MAX_MB * 1024 * 1024

# Optional: cookies.txt exported from a logged-in browser fixes most Instagram blocks.
COOKIES_FILE = Path(__file__).resolve().parent.parent / "cookies.txt"

URL_PATTERN = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.)?"
    r"(?:twitter\.com|x\.com|instagram\.com|instagr\.am|tiktok\.com|"
    r"youtube\.com/shorts|youtu\.be|youtube\.com/watch|reddit\.com|v\.redd\.it|"
    r"facebook\.com|fb\.watch|threads\.net)"
    r"/\S*",
    re.IGNORECASE,
)



def find_url(text: str):
    m = URL_PATTERN.search(text or "")
    return m.group(0) if m else None


ANY_URL = re.compile(r"https?://\S+", re.IGNORECASE)
DOWNLOAD_WORDS = re.compile(
    r"\b(download|downlaod|dl|save|mp4|reel ?file|video ?file|send (me )?(the |this )?(video|clip|reel)|"
    r"get (me )?(the |this )?(video|clip|reel)|grab)\b", re.IGNORECASE)

BARE_LINK_NOTE = ("(They sent only this link, no message. Open it with read_url and tell them what it is: a short "
                  "summary and the key points. For a video, use the transcript if there is one. Don't download it.)")


def any_url(text: str):
    m = ANY_URL.search(text or "")
    return m.group(0).rstrip(").,!?'\"") if m else None


def is_video_link(url: str) -> bool:
    """A site we can download videos from (X, Instagram, TikTok, YouTube, Reddit, Facebook, Threads)."""
    return bool(URL_PATTERN.match(url or ""))


def only_link(text: str) -> bool:
    """The message is just a link, nothing else."""
    url = any_url(text)
    return bool(url) and not (text or "").replace(url, "").strip(" \n\t.,!?:-")


def wants_download(text: str) -> bool:
    """A video link plus words asking for the file ('download', 'save', 'dl', 'send me the video')."""
    url = find_url(text)
    return bool(url) and bool(DOWNLOAD_WORDS.search((text or "").replace(url, " ")))


def is_bare_link(text: str) -> bool:
    """Kept for compatibility: now means 'a video link the user wants downloaded'."""
    return wants_download(text)


class DownloadFailed(Exception):
    pass


def probe_video(path: str) -> tuple:
    """(display width, display height, duration seconds) of the real file, or (0, 0, 0) if unknown."""
    try:
        import av
        with av.open(path) as c:
            st = c.streams.video[0]
            w, h = st.codec_context.width, st.codec_context.height
            sar = st.codec_context.sample_aspect_ratio or st.sample_aspect_ratio
            if sar and sar.numerator and sar.denominator and sar != 1:
                w = round(w * sar.numerator / sar.denominator)
            rot = 0
            try:
                rot = int(float(st.metadata.get("rotate", 0) or 0))
            except (TypeError, ValueError):
                pass
            if not rot:
                try:  # phone videos keep rotation in a display matrix; the first decoded frame reports it
                    for frame in c.decode(video=0):
                        rot = int(round(float(getattr(frame, "rotation", 0) or 0)))
                        break
                except Exception:
                    pass
            if abs(rot) % 180 == 90:
                w, h = h, w
            dur = int(float(c.duration) / 1_000_000) if c.duration else 0
            if w > 0 and h > 0:
                return w, h, dur
    except Exception:
        pass
    try:  # fall back to ffprobe if PyAV isn't available
        import json as _json
        import subprocess
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,sample_aspect_ratio:stream_side_data=rotation:stream_tags=rotate"
                              ":format=duration", "-of", "json", path], capture_output=True, text=True, timeout=20)
        d = _json.loads(out.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        sar = st.get("sample_aspect_ratio") or "1:1"
        if ":" in sar and sar not in ("1:1", "0:1"):
            a, b = sar.split(":")
            if int(b):
                w = round(w * int(a) / int(b))
        rot = int(float((st.get("tags") or {}).get("rotate") or 0))
        for sd in st.get("side_data_list") or []:
            rot = rot or int(float(sd.get("rotation") or 0))
        if abs(rot) % 180 == 90:
            w, h = h, w
        dur = int(float((d.get("format") or {}).get("duration") or 0))
        return (w, h, dur) if w > 0 and h > 0 else (0, 0, dur)
    except Exception:
        return 0, 0, 0


def _x_fallback(url: str, out_dir: str):
    """X/Twitter often breaks yt-dlp (guest tokens, login walls). FxTwitter's free public API returns the post's direct
    MP4 links; download the best one that fits. Returns (path, info) or None."""
    m = re.search(r"(?:twitter\.com|x\.com)/(?:[^/]+|i)/status(?:es)?/(\d+)", url)
    if not m:
        return None
    try:
        j = requests.get(f"https://api.fxtwitter.com/status/{m.group(1)}", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"}).json()
    except Exception:
        return None
    t = j.get("tweet") or {}
    vids = ((t.get("media") or {}).get("videos") or []) + [x for x in (t.get("media") or {}).get("all") or []
                                                            if x.get("type") in ("video", "gif")]
    links = []
    for v in vids:
        for f in sorted(v.get("formats") or [], key=lambda f: -(f.get("bitrate") or 0)):
            if f.get("container") == "mp4" or str(f.get("url", "")).split("?")[0].endswith(".mp4"):
                links.append(f["url"])
        if v.get("url"):
            links.append(v["url"])
    for link in dict.fromkeys(links):
        path = os.path.join(out_dir, f"{m.group(1)}.mp4")
        try:
            with requests.get(link, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    continue
                size = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        size += len(chunk)
                        if size > MAX_FILE_SIZE:
                            break
                        f.write(chunk)
            if 0 < size <= MAX_FILE_SIZE:
                return path, {"title": (t.get("text") or "")[:200], "description": t.get("text") or "",
                              "uploader": (t.get("author") or {}).get("name", ""), "duration": 0}
        except Exception:
            continue
    return None


def download_video(url: str) -> dict:
    """Blocking download. Returns {path, dir, title, duration, width, height, size_mb}.
    Caller must delete result['dir'] when done."""
    out_dir = tempfile.mkdtemp(prefix="agentvid_")
    limit = f"{MAX_MB - 2}M"
    opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "format": (
            f"best[ext=mp4][filesize<{limit}]/"
            f"best[filesize<{limit}]/"
            f"best[filesize_approx<{limit}]/"
            "best"
        ),
        "max_filesize": MAX_FILE_SIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 8,
        "retries": 3,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        fb = _x_fallback(url, out_dir) if re.search(r"(twitter|x)\.com/", url) else None
        if fb:
            path, info = Path(fb[0]), fb[1]
        else:
            shutil.rmtree(out_dir, ignore_errors=True)
            msg = str(e).lower()
            if "login" in msg or "rate" in msg:
                raise DownloadFailed(
                    "The site blocked this download (login or rate limit). "
                    "Adding a cookies.txt file to the my-agent folder usually fixes Instagram."
                )
            if "no video" in msg:
                raise DownloadFailed("No video found in that post.")
            raise DownloadFailed(f"Download failed: {str(e)[:250]}")

    if not path.exists():
        files = [p for p in Path(out_dir).iterdir() if p.is_file()]
        if not files:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise DownloadFailed(
                f"No file was saved. The video is probably over the {MAX_MB} MB limit."
            )
        path = max(files, key=lambda p: p.stat().st_size)

    size = path.stat().st_size
    if size > MAX_FILE_SIZE:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise DownloadFailed(
            f"Video is {size / 1048576:.0f} MB, over the {MAX_MB} MB Telegram bot limit. "
            "Use @pesvideodownloader_bot for big files."
        )

    w, h, dur = probe_video(str(path))
    return {
        "path": str(path),
        "dir": out_dir,
        "title": (info.get("title") or "")[:200],
        "duration": dur or int(info.get("duration") or 0),
        # Size as it should be displayed, measured from the actual file (rotation and non-square pixels
        # included). 0 = unknown: Telegram then works it out itself instead of stretching the video.
        "width": w,
        "height": h,
        "size_mb": round(size / 1048576, 1),
        "description": (info.get("description") or "")[:1500],
        "uploader": info.get("uploader") or "",
    }


def tool_download_video(url: str) -> str:
    """Agent tool entry point: downloads and queues the file for the current chat."""
    from core import context
    ctx = context.get()
    if ctx.platform != "telegram":
        return "Video downloading only works on Telegram right now. Tell the user to send the link there."
    try:
        result = download_video(url)
    except DownloadFailed as e:
        return str(e)
    ctx.outbox.append({"kind": "video", **result})
    parts = [
        f"Downloaded ({result['size_mb']} MB, {result['duration']}s). "
        "It will be sent to the user automatically right after your reply; do not say you cannot send files.",
    ]
    if result["title"]:
        parts.append(f"Title: {result['title']}")
    if result["uploader"]:
        parts.append(f"Uploader: {result['uploader']}")
    if result["description"]:
        parts.append(f"Caption/description: {result['description']}")
    return "\n".join(parts)


def cleanup(result: dict):
    shutil.rmtree(result.get("dir", ""), ignore_errors=True)
