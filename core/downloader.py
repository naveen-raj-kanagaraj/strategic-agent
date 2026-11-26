"""Video downloading for the agent (ported from telegram-video-bot).

Two ways in:
  1. Fast path: the Telegram handler sees a bare link and calls download_video() directly.
  2. Agent tool: Claude calls the download_video tool; the file is queued in OUTBOX and the
     Telegram handler sends it after the reply.
"""
import copy
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
# Highest resolution to aim for, measured on the short side (1080 = 1080p, also for vertical 1080x1920 reels).
MAX_RES = int(os.getenv("VIDEO_MAX_RES", "1080"))
# ffmpeg joins separate video and audio streams. YouTube (and often Instagram) only offer HD that way;
# without ffmpeg the best single file with sound is usually 360p. Install with: brew install ffmpeg
FFMPEG = shutil.which("ffmpeg")
# Old selector, kept as the last resort: the best single file that already has sound.
LEGACY_FORMAT = (f"best[ext=mp4][filesize<{MAX_MB - 2}M]/best[filesize<{MAX_MB - 2}M]/"
                 f"best[filesize_approx<{MAX_MB - 2}M]/best")

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


def _size(f: dict, duration: float):
    """Bytes for one format: exact, approximate, or bitrate x duration. None if unknown."""
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return float(s)
    tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
    return tbr * 1000 / 8 * duration if tbr and duration else None


def _short_side(f: dict) -> int:
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else (h or w or 0)


def _plays_in_telegram(f: dict) -> bool:
    """H.264 video and AAC audio play inline everywhere, iPhone included. Unknown codecs (common on X and Instagram
    progressive files) are almost always H.264/AAC too."""
    v, a = (f.get("vcodec") or "").lower(), (f.get("acodec") or "").lower()
    return (v in ("", "none") or v.startswith(("avc", "h264"))) and (a in ("", "none") or a.startswith(("mp4a", "aac")))


def choose_formats(info: dict, have_ffmpeg: bool, budget: float, max_res: int = MAX_RES) -> list:
    """Best-first list of (yt-dlp format spec, label) that should fit in budget bytes.

    Ranking: known size first, then plays inline in Telegram (H.264/AAC), then resolution, then bitrate. With ffmpeg,
    separate video + audio streams compete with single files, which is what lifts YouTube from 360p to 1080p."""
    dur = float(info.get("duration") or 0)
    fmts = [f for f in info.get("formats") or [] if f.get("format_id") and not f.get("has_drm")
            and f.get("ext") != "mhtml" and f.get("protocol") != "mhtml"]
    videos = [f for f in fmts if f.get("acodec") == "none" and f.get("vcodec") != "none"]
    audios = [f for f in fmts if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    singles = [f for f in fmts if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    ranked = []

    def add(spec, label, est, plays, short, tbr):
        if est is not None and est > budget:
            return
        if short and short > max_res:
            return
        ranked.append(((est is not None, plays, short, tbr or 0), spec, label))

    if have_ffmpeg and videos and audios:
        aac = [a for a in audios if _plays_in_telegram({"acodec": a.get("acodec"), "vcodec": "avc"})]
        pool = aac or audios
        # original-language track first (YouTube dubs), then not volume-compressed, then bitrate
        audio = max(pool, key=lambda a: (a.get("language_preference") or 0, "drc" not in str(a.get("format_id")),
                                         a.get("abr") or a.get("tbr") or 0))
        a_size = _size(audio, dur) or (16000 * dur if dur else None)   # ~128 kbps if unknown
        for v in videos:
            v_size = _size(v, dur)
            est = v_size + a_size if v_size is not None and a_size is not None else None
            add(f"{v['format_id']}+{audio['format_id']}", f"{_short_side(v) or '?'}p video + audio", est,
                _plays_in_telegram({"vcodec": v.get("vcodec"), "acodec": audio.get("acodec")}),
                _short_side(v), v.get("tbr") or v.get("vbr"))
    for f in singles:
        add(f["format_id"], f"{_short_side(f) or '?'}p single file", _size(f, dur), _plays_in_telegram(f),
            _short_side(f), f.get("tbr"))
    ranked.sort(key=lambda r: r[0], reverse=True)
    seen, out = set(), []
    for _, spec, label in ranked:
        if spec not in seen:
            seen.add(spec)
            out.append((spec, label))
    return out


def _base_opts(out_dir: str) -> dict:
    opts = {
        "outtmpl": os.path.join(out_dir, "%(id)s.%(ext)s"),
        "max_filesize": MAX_FILE_SIZE,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 8,
        "retries": 3,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["merge_output_format"] = "mp4"
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def _saved_file(out_dir: str, expected: Path):
    if expected.exists():
        return expected
    files = [p for p in Path(out_dir).iterdir()   # skip partial files and unmerged pieces like id.f137.mp4
             if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))
             and not (len(p.suffixes) > 1 and p.suffixes[-2].startswith(".f"))]
    return max(files, key=lambda p: p.stat().st_size) if files else None


# Video-level fields that must survive when the format is chosen again.
_KEEP = {"id", "title", "fulltitle", "duration", "duration_string", "extractor", "extractor_key", "webpage_url",
         "original_url", "webpage_url_basename", "webpage_url_domain", "display_id", "uploader", "uploader_id",
         "channel", "description", "timestamp", "upload_date", "thumbnail", "thumbnails", "formats", "_type",
         "playlist", "playlist_index", "epoch", "_version"}


def _reselectable(info: dict) -> dict:
    """A copy of an extracted video with yt-dlp's first format pick removed. extract_info() copies the chosen
    format's fields (url, requested_formats, size...) onto the video; left in place, a second pass would quietly
    download that first pick instead of the one asked for."""
    clean = copy.deepcopy(info)
    stale = {"requested_formats", "requested_downloads", "format", "format_id"}
    for f in (clean.get("formats") or []) + (clean.get("requested_formats") or []):
        stale |= set(f)
    for k in stale - _KEEP:
        clean.pop(k, None)
    return clean


def _clear(out_dir: str):
    for p in Path(out_dir).iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


def download_best(info: dict, out_dir: str, base: dict = None, attempts: int = 4) -> tuple:
    """Download the best quality that fits under MAX_MB from an already extracted info dict.
    Tries the ranked formats in order (a too-big result is thrown away and the next one tried), then the old selector.
    Returns (path or None, info, label). Raises yt_dlp DownloadError if every attempt errored."""
    base = base or _base_opts(out_dir)
    budget = (MAX_MB - 2) * 1024 * 1024
    tries = choose_formats(info, bool(base.get("ffmpeg_location")), budget)[:attempts] + [(LEGACY_FORMAT, "fallback")]
    last_err = None
    for spec, label in tries:
        _clear(out_dir)
        try:
            with yt_dlp.YoutubeDL({**base, "format": spec}) as ydl:
                res = ydl.process_ie_result(_reselectable(info), download=True)
                path = _saved_file(out_dir, Path(ydl.prepare_filename(res)))
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            continue
        if path and path.stat().st_size <= MAX_FILE_SIZE:
            print(f"Download: {label} [{spec}], {path.stat().st_size / 1048576:.1f} MB")
            return path, res, label
        last_err = None   # skipped or over the limit: try the next, smaller choice
    _clear(out_dir)
    if last_err:
        raise last_err
    return None, info, ""


def download_video(url: str) -> dict:
    """Blocking download. Returns {path, dir, title, duration, width, height, size_mb}.
    Caller must delete result['dir'] when done."""
    out_dir = tempfile.mkdtemp(prefix="agentvid_")
    base = _base_opts(out_dir)
    path = None
    try:
        with yt_dlp.YoutubeDL(base) as ydl:
            info = ydl.extract_info(url, download=False)
        if info.get("entries") is not None:          # posts with several videos: take the first
            info = next((e for e in info["entries"] if e), None)
            if not info:
                raise yt_dlp.utils.DownloadError("No video found in that post.")
        path, info, _ = download_best(info, out_dir, base)
        if path is None:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise DownloadFailed(
                f"No version of this video fits Telegram's {MAX_MB} MB bot limit. "
                "Use @pesvideodownloader_bot for big files."
            )
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
    if not ctx.is_owner:
        return "Video downloads are only available to the bot owner."
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
