"""Reading links: web pages, PDFs and YouTube transcripts."""
import ipaddress
import re
import socket
from urllib.parse import urlparse, parse_qs

import requests

MAX_CHARS = 15000
MAX_BYTES = 5 * 1024 * 1024
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _is_public_host(host: str) -> bool:
    """Block localhost, LAN and other private addresses so nobody can make the bot
    fetch pages from the owner's own network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def youtube_id(url: str):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        if p.path == "/watch":
            return parse_qs(p.query).get("v", [None])[0]
        m = re.match(r"^/(shorts|live|embed)/([\w-]{6,})", p.path)
        if m:
            return m.group(2)
    return None


def youtube_transcript(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en", "en-IN", "hi", "ta"])
    except Exception:
        # Fall back to whatever language exists, translated to English if possible.
        tl = api.list(video_id)
        t = next(iter(tl))
        try:
            t = t.translate("en")
        except Exception:
            pass
        fetched = t.fetch()
    return " ".join(s.text for s in fetched).strip()


def _html_to_text(html: str) -> tuple:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "aside", "svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n", text)
    desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc and desc.get("content"):
        text = desc["content"].strip() + "\n\n" + text
    return title, text


def _n(x) -> str:
    if x is None:
        return "?"
    x = float(x)
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if x >= div:
            return f"{x / div:.1f}{unit}".replace(".0", "")
    return str(int(x))


def _tweet(url: str):
    """X/Twitter posts need a login to read; FxTwitter's public API returns the text and stats."""
    m = re.search(r"(?:twitter|x)\.com/([^/]+)/status(?:es)?/(\d+)", url)
    if not m:
        return None
    r = requests.get(f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}",
                     headers={"User-Agent": UA}, timeout=15)
    if r.status_code != 200:
        return None
    t = r.json().get("tweet") or {}
    if not t:
        return None
    a = t.get("author") or {}
    media = t.get("media") or {}
    kinds = [k for k in ("videos", "photos") if media.get(k)]
    out = [f"Post on X by {a.get('name', '')} (@{a.get('screen_name', '')}), {t.get('created_at', '')}",
           f"Likes {_n(t.get('likes'))}, reposts {_n(t.get('retweets'))}, replies {_n(t.get('replies'))}, "
           f"views {_n(t.get('views'))}" + (f"; contains {' and '.join(kinds)}" if kinds else ""),
           "", t.get("text", "")]
    q = t.get("quote")
    if q:
        out += ["", f"Quoting @{(q.get('author') or {}).get('screen_name', '')}: {q.get('text', '')}"]
    return "\n".join(out)


def _reddit_thread(url: str):
    host = (urlparse(url).hostname or "").lower()
    if "/comments/" not in url or not (host == "reddit.com" or host.endswith(".reddit.com")):
        return None
    r = requests.get(url.split("?")[0].rstrip("/") + ".json", params={"limit": 15, "sort": "top"},
                     headers={"User-Agent": "pesagent/1.0 (link reader)"}, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    post = data[0]["data"]["children"][0]["data"]
    out = [f"Reddit post in r/{post.get('subreddit')} by u/{post.get('author')}: {post.get('title')}",
           f"Score {_n(post.get('score'))}, {_n(post.get('num_comments'))} comments", "",
           (post.get("selftext") or post.get("url") or "")[:6000], "", "Top comments:"]
    for c in data[1]["data"]["children"][:12]:
        d = c.get("data", {})
        if d.get("body"):
            out.append(f"- ({_n(d.get('score'))}) {d['body'][:500]}")
    return "\n".join(out)


def _video_info(url: str):
    """Caption and stats for a video post (Instagram, TikTok, YouTube, Facebook, Threads) without downloading it."""
    import yt_dlp
    from core.downloader import COOKIES_FILE
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 20}
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    with yt_dlp.YoutubeDL(opts) as y:
        i = y.extract_info(url, download=False)
    if i.get("entries"):
        i = next((e for e in i["entries"] if e), i)
    dur = i.get("duration")
    date = i.get("upload_date") or ""
    lines = [f"{i.get('extractor_key') or 'Video'} post by {i.get('uploader') or i.get('channel') or '?'}"
             + (f", {date[:4]}-{date[4:6]}-{date[6:]}" if len(date) == 8 else ""),
             f"Title: {i.get('title') or ''}",
             f"Views {_n(i.get('view_count'))}, likes {_n(i.get('like_count'))}, comments {_n(i.get('comment_count'))}"
             + (f", length {int(dur // 60)}:{int(dur % 60):02d}" if dur else ""),
             "", "Caption / description:", (i.get("description") or "(none)")[:5000]]
    return "\n".join(lines)


def social_post(url: str):
    """Best-effort reading of social links that normally need a login. None if nothing worked."""
    from core.downloader import is_video_link
    for fn in (_tweet, _reddit_thread):
        try:
            got = fn(url)
            if got:
                return got
        except Exception:
            pass
    if is_video_link(url):
        try:
            return _video_info(url)
        except Exception as e:
            return f"Couldn't read that post ({str(e)[:150]}). It may be private or need a login."
    return None


def read_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    if not host or not _is_public_host(host):
        return "I can't open that address (only public websites are allowed)."

    vid = youtube_id(url)
    if vid:
        info = ""
        try:
            info = _video_info(url)
        except Exception:
            pass
        try:
            text = youtube_transcript(vid)
            return (info + "\n\n" if info else "") + f"Transcript ({len(text.split())} words):\n{text[:MAX_CHARS]}"
        except Exception as e:
            if info:
                return info + f"\n\n(No transcript available: {str(e)[:100]}. Summarise from the title and description.)"
            return f"No transcript available for that YouTube video ({str(e)[:120]})."

    social = social_post(url)
    if social:
        return social

    from core import webfetch
    if webfetch.liquipedia_page(url):   # Liquipedia blocks plain scrapers; its API is the sanctioned way in
        try:
            title, text = _html_to_text(webfetch.get(url))
            return (f"Liquipedia page (read through its API)\nURL: {url}\n\n{text[:MAX_CHARS]}\n\n(To download its "
                    "logos or images, use run_python: outline(url) and images_by_section(url) list them by section.)")
        except Exception as e:
            return f"Couldn't read that Liquipedia page: {str(e)[:200]}"

    try:
        current = url
        for _ in range(6):  # follow redirects manually, checking every hop
            r = requests.get(current, headers={"User-Agent": UA, "Accept-Language": "en"}, timeout=20,
                             stream=True, allow_redirects=False)
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                nxt = requests.compat.urljoin(current, r.headers.get("location", ""))
                if not _is_public_host(urlparse(nxt).hostname or ""):
                    return "That link redirects to a private address, so I won't open it."
                current = nxt
                continue
            break
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > MAX_BYTES:
                break
    except requests.RequestException as e:
        return f"Couldn't open the link: {str(e)[:150]}"

    if r.status_code >= 400:
        try:   # second try with full browser headers before giving up
            html = webfetch.get(url)
            title, text = _html_to_text(html)
            if len(text) >= 200:
                return f"Title: {title}\nURL: {url}\n\n{text[:MAX_CHARS]}"
        except Exception:
            pass
        return (f"The site returned an error ({r.status_code}) even with browser headers. It may need a login. "
                "Say so plainly; don't guess what the page says.")

    ctype = r.headers.get("content-type", "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            text = "".join(page.get_text() for page in doc)
            return f"PDF ({doc.page_count} pages):\n{text[:MAX_CHARS]}"
        except Exception as e:
            return f"Couldn't read that PDF: {str(e)[:150]}"

    if "html" in ctype or "xml" in ctype or not ctype:
        title, text = _html_to_text(data.decode(r.encoding or "utf-8", errors="ignore"))
        if len(text) < 200:
            return (f"Title: {title}\nThe page had almost no readable text. It probably needs a login "
                    f"(common for LinkedIn, Instagram and X) or loads with JavaScript.\n{text}")
        return f"Title: {title}\nURL: {r.url}\n\n{text[:MAX_CHARS]}"

    if ctype.startswith("text/"):
        return data.decode("utf-8", errors="ignore")[:MAX_CHARS]
    return f"That link is a {ctype or 'binary'} file, which I can't read."
