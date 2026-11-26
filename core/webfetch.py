"""Fetching web pages and files for jobs like "download all the logos on this page, sort them by group, send a zip".

Used by read_url and, inside run_python, as ready-made helpers:
    html = get(url)                  page text (or bytes with binary=True)
    soup = page(url)                 BeautifulSoup of the page
    imgs = images_by_section(url)    every image in page order: section, tab, table, name, full-size src, kind
    print(outline(url))              short summary of the above, grouped (cheap to read before writing more code)
    save(src, "logos/Finals/ULF Esports.png")   download a file, folders created as needed

Liquipedia blocks plain scrapers but publishes an API for exactly this; its pages are read through that API, at the
pace its terms ask for (one page request per 30 seconds, others 2 seconds apart), and cached for 10 minutes.
No secrets here: run_python imports this in a process without the agent's keys.
"""
import hashlib
import json
import os
import re
import tempfile
import time
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}
# Liquipedia's API terms ask for a user agent that names the app and how to reach its owner.
LIQUIPEDIA_UA = "PesAgent/1.0 (personal research assistant; https://github.com/naveen-raj-kanagaraj/strategic-agent)"
MAX_BYTES = 40 * 1024 * 1024
CACHE_DIR = os.path.join(tempfile.gettempdir(), "pesagent_webcache")
CACHE_SECONDS = 600


class FetchError(Exception):
    pass


# ---------- helpers ----------

def _public(url: str) -> None:
    from core.web import _is_public_host
    host = urlparse(url).hostname or ""
    if not host or not _is_public_host(host):
        raise FetchError(f"Not a public website: {url}")


def _wait(key: str, gap: float) -> None:
    """Keep at least `gap` seconds between requests of one kind, across separate run_python calls."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    stamp = os.path.join(CACHE_DIR, f"last_{key}")
    try:
        last = os.path.getmtime(stamp)
    except OSError:
        last = 0
    delay = last + gap - time.time()
    if 0 < delay <= gap:
        time.sleep(delay)
    with open(stamp, "w"):
        pass
    os.utime(stamp, None)


def _cache(key: str, value: str = None):
    path = os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest() + ".html")
    if value is None:
        try:
            if time.time() - os.path.getmtime(path) < CACHE_SECONDS:
                with open(path, encoding="utf-8") as f:
                    return f.read()
        except OSError:
            return None
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    return value


def liquipedia_page(url: str):
    """(wiki, page title) for a liquipedia.net page link, else None."""
    p = urlparse(url)
    if not (p.hostname or "").endswith("liquipedia.net"):
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or parts[0] == "commons" or parts[1] in ("api.php", "index.php"):
        return None   # image files and API calls aren't pages
    return parts[0], unquote("/".join(parts[1:]))


def _liquipedia_html(wiki: str, title: str) -> str:
    cached = _cache(f"lp:{wiki}:{title}")
    if cached:
        return cached
    _wait("liquipedia_parse", 30)
    r = requests.get(f"https://liquipedia.net/{wiki}/api.php",
                     params={"action": "parse", "page": title, "format": "json", "formatversion": "2",
                             "prop": "text", "redirects": "1", "disableeditsection": "1"},
                     headers={"User-Agent": LIQUIPEDIA_UA, "Accept-Encoding": "gzip"}, timeout=30)
    if r.status_code != 200:
        raise FetchError(f"Liquipedia API returned {r.status_code} for {title}")
    j = r.json()
    if "error" in j:
        raise FetchError(f"Liquipedia: {j['error'].get('info', j['error'])}")
    html = (j.get("parse") or {}).get("text") or ""
    if isinstance(html, dict):
        html = html.get("*", "")
    return _cache(f"lp:{wiki}:{title}", html)


# ---------- public helpers ----------

def get(url: str, binary: bool = False, timeout: int = 30):
    """Fetch a page (text) or file (bytes). Liquipedia pages come through its API; other sites get full browser
    headers and one retry. Raises FetchError with the reason."""
    if not re.match(r"^https?://", url or ""):
        url = "https://" + (url or "")
    _public(url)
    lp = liquipedia_page(url)
    if lp and not binary:
        return _liquipedia_html(*lp)
    if not binary:
        cached = _cache(f"url:{url}")
        if cached:
            return cached
    if (urlparse(url).hostname or "").endswith("liquipedia.net"):
        _wait("liquipedia", 1 if "/commons/images/" in url else 2)
    last = None
    for attempt in range(2):
        try:
            headers = dict(BROWSER)
            if (urlparse(url).hostname or "").endswith("liquipedia.net"):
                headers["User-Agent"] = LIQUIPEDIA_UA
            r = requests.get(url, headers=headers, timeout=timeout, stream=True)
            if r.status_code in (429, 503) and attempt == 0:
                time.sleep(min(int(r.headers.get("retry-after", "5") or 5), 20))
                continue
            if r.status_code >= 400:
                raise FetchError(f"{r.status_code} from {urlparse(url).hostname}")
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise FetchError(f"File over {MAX_BYTES // 1048576} MB: {url}")
            if binary:
                return data
            text = data.decode(r.encoding or "utf-8", errors="ignore")
            return _cache(f"url:{url}", text)
        except requests.RequestException as e:
            last = e
            time.sleep(2)
    raise FetchError(f"Couldn't fetch {url}: {last}")


def page(url: str):
    """BeautifulSoup of a page, with links and image sources made absolute."""
    from bs4 import BeautifulSoup
    base = "https://liquipedia.net/" if liquipedia_page(url) else url
    soup = BeautifulSoup(get(url), "lxml")
    for el in soup.find_all("img"):   # lazy-loaded images keep the real address in data-src
        real = el.get("data-src") or el.get("data-lazy-src")
        if real and (not el.get("src") or el["src"].startswith("data:")):
            el["src"] = real
    for tag, attr in (("img", "src"), ("a", "href")):
        for el in soup.find_all(tag):
            if el.get(attr) and not el[attr].startswith("data:"):
                el[attr] = urljoin(base, el[attr])
    return soup


def full_size(src: str) -> str:
    """MediaWiki thumbnail -> original file (Liquipedia, Wikipedia): .../thumb/a/ab/X.png/50px-X.png -> .../a/ab/X.png"""
    m = re.match(r"^(.*?/images)/thumb/(.+?/[^/]+\.\w+)/[^/]+$", src)
    return f"{m.group(1)}/{m.group(2)}" if m else src


def _heading_text(el) -> str:
    return re.sub(r"\s*\[\s*edit\s*\]\s*$", "", el.get_text(" ", strip=True)).strip()


def _tab_label(img) -> str:
    """Liquipedia tabs: <div class="tabs-dynamic"><ul><li class="tab2">Group Red</li></ul>...<div class="content2">"""
    for div in img.find_parents("div"):
        cls = " ".join(div.get("class") or [])
        m = re.search(r"\bcontent(\d+)\b", cls)
        if m:
            for box in div.find_parents("div"):   # the tab names sit in the tabs box around the content panes
                li = box.find("li", class_=re.compile(rf"\btab{m.group(1)}\b"))
                if li:
                    return li.get_text(" ", strip=True)
    return ""


def images_by_section(url: str, dedupe: bool = True) -> list:
    """Every image on the page in order, with where it sits:
    {"section": "Participants > Gauntlet", "tab": "Group Green", "table": "Group Green", "name": "ULF Esports",
     "src": full-size image URL, "kind": "team" | "flag" | "icon" | "image", "variant": "light" | "dark"}
    With dedupe, a team or image that appears twice in the same place (light and dark logo) is kept once, light."""
    soup = page(url)
    levels, out = {}, []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "img"]):
        if el.name != "img":
            n = int(el.name[1])
            levels = {k: v for k, v in levels.items() if k < n}
            levels[n] = _heading_text(el)
            continue
        src = el.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        anc = " ".join(" ".join(p.get("class") or []) for p in el.find_parents(limit=4))
        link = el.find_parent("a")
        name = (el.get("alt") or (link.get("title") if link else "") or "").strip()
        lower = (src + " " + anc).lower()
        near = " ".join(" ".join(p.get("class") or []) for p in el.find_parents(limit=2)).lower()
        kind = ("flag" if "flag" in near or re.search(r"/[a-z]{2}_hd\.png$", src.lower())
                else "team" if "team-template" in anc or "teamcard" in lower or "_logo" in lower or "logo" in name.lower()
                else "flag" if "flag" in lower else "icon" if re.search(r"/(?:\d{1,2})px-", src) else "image")
        table = el.find_parent("table")
        th = table.find("th") if table else None
        out.append({"section": " > ".join(levels[k] for k in sorted(levels)), "tab": _tab_label(el),
                    "table": th.get_text(" ", strip=True)[:80] if th else "", "name": name,
                    "src": full_size(src), "kind": kind,
                    "variant": "dark" if "darkmode" in lower else "light"})
    if not dedupe:
        return out
    best = {}
    for i, r in enumerate(out):
        key = (r["section"], r["tab"], r["table"], r["name"] or r["src"])
        if key not in best or (best[key][1]["variant"] == "dark" and r["variant"] == "light"):
            best[key] = (best.get(key, (i,))[0], r)
    return [r for _, r in sorted(best.values(), key=lambda x: x[0])]


def outline(url: str, kind: str = None) -> str:
    """Images grouped by section / tab / table, names only: read this first, then pick what to download."""
    groups = {}
    for r in images_by_section(url):
        if kind and r["kind"] != kind:
            continue
        where = " | ".join(x for x in (r["section"], r["tab"], r["table"]) if x) or "(top)"
        groups.setdefault(where, []).append(r["name"] or os.path.basename(r["src"]))
    return "\n".join(f"{w}  [{len(n)}]: {', '.join(n[:40])}" for w, n in groups.items()) or "No images found."


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", name).strip()[:100] or "file"


def save(url: str, path: str) -> str:
    """Download a file to path (folders created). Adds the right extension if path has none. Returns the path."""
    data = get(url, binary=True)
    if not os.path.splitext(path)[1]:
        ext = os.path.splitext(urlparse(url).path)[1] or ".bin"
        path += ext.lower()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path
