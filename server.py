import json
import os
import random
import re
import socket
import time
import threading
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

PORT = int(os.environ.get("PORT", 8000))

RSS_FEEDS = [
    # Al Jazeera — article links scraped for og:image URLs.
    "https://www.aljazeera.com/xml/rss/all.xml",

    # BBC: backup only, capped low.
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/rss.xml",

    # NPR
    "https://feeds.npr.org/1001/rss.xml",
    "https://feeds.npr.org/1004/rss.xml",
    "https://feeds.npr.org/1003/rss.xml",
]

SOURCE_PAGES = [
    "https://www.theguardian.com/world",
    "https://www.theguardian.com/us-news",
    "https://www.theguardian.com/politics",
    "https://www.theguardian.com/environment",
    "https://www.theguardian.com/global-development",
    "https://www.theguardian.com/science",
    "https://www.theguardian.com/society",
    "https://www.theguardian.com/technology",
    "https://www.npr.org/sections/world/",
    "https://www.npr.org/sections/national/",
    "https://www.npr.org/sections/politics/",
    "https://www.npr.org/sections/health/",
]


# Direct public section pages. These are scraped for image URLs because several
# non-BBC sources do not expose usable images through RSS.
DIRECT_IMAGE_PAGES = []


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_IMAGE_POOL = 900
SEQUENCE_LENGTH = 800
IMAGE_CACHE = {"time": 0, "images": [], "lock": threading.Lock()}
CACHE_SECONDS = 120
BACKGROUND_REFRESH_SECONDS = 120  # pre-warm interval

PROXY_CACHE = {}
PROXY_CACHE_SECONDS = 600
PROXY_CACHE_MAX_ITEMS = 200

REJECT_CACHE = {}
REJECT_CACHE_SECONDS = 1800

# URLs that passed cv2 checks during pool build — skip checks at serve time.
APPROVED_URLS = set()

GUARDIAN_API_ENABLED = True
GUARDIAN_API_KEY = "55e2b57b-70cb-4542-a4b4-83971a720752"
GUARDIAN_API_SECTIONS = [
    "world", "us-news", "politics", "environment",
    "global-development", "immigration", "science",
    "society", "technology", "business", "culture",
]


def fetch_guardian_api_images(limit=200):
    """Fetch images from Guardian open content API — structured, no scraping needed."""
    images = []
    seen = set()
    for section in GUARDIAN_API_SECTIONS:
        if len(images) >= limit:
            break
        try:
            url = (
                f"https://content.guardianapis.com/search"
                f"?section={section}&show-fields=main&page-size=50"
                f"&order-by=newest&api-key={GUARDIAN_API_KEY}"
            )
            data = fetch_text(url, timeout=6)
            blob = json.loads(data)
            results = blob.get("response", {}).get("results", [])
            for item in results:
                fields = item.get("fields", {})
                img_url = fields.get("main", "")
                if not img_url:
                    continue
                # Guardian main field returns HTML — extract the src URL.
                src_match = re.search(r'src="([^"]+)"', img_url)
                if src_match:
                    img_url = src_match.group(1)
                # Skip staff avatars and tiny images.
                if "/img/uploads/" in img_url or "/img/static/" in img_url:
                    continue
                # i.guim.co.uk is the resizing CDN used for thumbnails/bylines.
                # Real news photos are on media.guim.co.uk.
                if "i.guim.co.uk" in img_url or "interactive.guim.co.uk" in img_url or "static.theguardian.com" in img_url:
                    continue
                # Upgrade to large size.
                img_url = re.sub(r'width=\d+', 'width=2000', img_url)
                cleaned = clean_extracted_image_url(img_url)
                if not cleaned or url_is_known_bad(cleaned):
                    continue
                key = normalize_image_url_for_dedupe(cleaned)
                if key and key not in seen:
                    seen.add(key)
                    images.append(cleaned)
                    if len(images) >= limit:
                        return images
        except Exception as e:
            print(f"[Guardian API] {section} error: {e}", flush=True)
            if "429" in str(e):
                print("[Guardian API] Rate limited — stopping for this cycle", flush=True)
                break
        time.sleep(1.5)  # avoid rate limiting
    print(f"[Guardian API] fetched {len(images)} images", flush=True)
    return images

GUARDIAN_API_CACHE = {"images": [], "time": 0}
GUARDIAN_API_CACHE_SECONDS = 3600  # 1 hour


def get_guardian_api_images():
    if not GUARDIAN_API_ENABLED:
        return []
    now = time.time()
    if GUARDIAN_API_CACHE["images"] and now - GUARDIAN_API_CACHE["time"] < GUARDIAN_API_CACHE_SECONDS:
        print(f"[Guardian API] Using cache: {len(GUARDIAN_API_CACHE['images'])} images", flush=True)
        return GUARDIAN_API_CACHE["images"][:]
    print("[Guardian API] Fetching fresh images …", flush=True)
    images = fetch_guardian_api_images(limit=400)
    print(f"[Guardian API] Got {len(images)} images", flush=True)
    if images:
        GUARDIAN_API_CACHE["images"] = images
        GUARDIAN_API_CACHE["time"] = now
    return images
MIN_IMAGE_WIDTH = 800
MIN_IMAGE_HEIGHT = 540

KNOWN_BAD_URL_FRAGMENTS = [
    "p0l7jnbt", "p0kxxp17", "p0n9y769", "3a08bc10", "b9785300",
    "4c3e0ce0", "9f35a8f0", "c471ab80",
    "c5a74450", "f53b6250", "p0ngd4cc",
    "00a03cc0", "929fd780", "06449360", "f4ee5fc0",
    "cfcd74b0", "7488a0b0", "72e83b70", "acb55400",
    "5a8f0590",
    "3a45b6139f0e4811b83b67069b3ba3f8",
    "5d8ee630ae7547f484839522c5309acd",
    "412719e44ad6a73780cf389a229b",
    "be77c61645479209fe360b0dfc79",
    "0e6e82f4ed2b66a75b5c6beb62b2",
    "bb93630408c744d6b8c58db130e5743f",
    "8222aecb2bf94a39b6f80b1efde09e2c",
    "facebook-default-wide",
    "a0b3c0e01f2f4a38802002a69e896fd5",
    "b713f599330d4bb490048b117f0b3dcc",
    "965f7a604adc96b5f4fe201c77c8",
    "f71df60c287b45bdb8f6e93cf9b1ca2b",
    "d37c3f215c3a40c9bb9c37c6e2e6bdd5",
    "4885843ae10b48aa9c1f8de09b6b1f86",
    "f18450782e9244acbe1c080840e9ab7a",
    "af6c0ee0",
    "11127980",
    "d1e71250",
    "60b2cc02514450d7361aa7a4c828fd2b830e10a1",
    "69d08f3b6f4e22f7b593c3747049897c05f3c35a",
    "e46b9940",
    "409aac70",
    "62f61850",
    "b4133310",
    "25317530",
    "5d62c560",
    "ef90228cd91038b433059da0b2a8481036ee2986",
    "a66a935e60878e7844fa2d1051c9f0144b334d9c",
    "a983c310",
    "73d3ef6a2b06188911f5c7e1a5c6480e935e942d",
    "bf0f4d3fe009178469f4c44167b12a3e857fd72a",
    "374c252854b26a9d2508b2fcfd25097469852efb",
    "a15c3d426bf3ab77265f232394e5eccb3f7f96af",
    "40755c81-979d-4d99-a472-2258517838b3",
]

VERTICAL_ONLY_URL_FRAGMENTS = [
    "166137e0", "9a3df7e0", "b1e9ef60", "843ef730",
    "7b23ccb0", "c022fa90", "679152b0", "3600d2f0",
    "ce4b17d0",
]

VOICE_CROP_URL_FRAGMENTS = ["f16b6b80"]


def url_is_known_bad(url):
    return any(fragment in url for fragment in KNOWN_BAD_URL_FRAGMENTS)


def url_is_vertical_only(url):
    return any(fragment in url for fragment in VERTICAL_ONLY_URL_FRAGMENTS)


def url_needs_voice_crop(url):
    return any(fragment in url for fragment in VOICE_CROP_URL_FRAGMENTS)


def upgrade_bbc_image_url(url):
    if not url:
        return url
    url = url.replace("/240/", "/1024/")
    url = url.replace("/320/", "/1024/")
    url = url.replace("/480/", "/1024/")
    url = url.replace("/624/", "/1024/")
    url = url.replace("/660/", "/1024/")
    url = re.sub(r"/ic/\d+x\d+/", "/ic/1024x576/", url)
    url = re.sub(r"/standard/\d+/", "/standard/1024/", url)
    return url


def clean_extracted_image_url(url):
    if not url:
        return None
    url = url.strip().replace("&amp;", "&")
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    lower = url.lower()
    if any(bad in lower for bad in ["logo", "placeholder", "blank", "sprite", "icon"]):
        return None
    # Reject SVG files.
    if lower.endswith(".svg") or ".svg?" in lower:
        return None
    # Reject sports and entertainment images by filename keywords.
    sports_entertainment_terms = [
        "nba", "nfl", "nhl", "mlb", "nascar", "soccer", "football", "basketball",
        "baseball", "hockey", "tennis", "golf", "olympics", "superbowl", "super-bowl",
        "grammy", "oscar", "emmy", "bafta", "cannes", "eurovision", "celebrity",
        "kardashian", "taylor-swift", "beyonce", "movie-poster", "film-poster",
    ]
    fname_lower = urllib.parse.urlparse(url).path.lower()
    if any(t in fname_lower for t in sports_entertainment_terms):
        return None
    # BBC photos are always .jpg — .png from BBC is always a graphic/illustration.
    if "bbci.co.uk" in lower and lower.endswith(".png"):
        return None
    if "spiegel.de" in lower and lower.endswith(".png"):
        return None
    # BBC /images/ic/ URLs with programme IDs (p0...) are show/podcast assets, not news photos.
    if "bbci.co.uk/images/ic/" in lower and "/p0" in lower:
        return None
    return upgrade_bbc_image_url(url)


def canonical_image_key(url):
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    inner = qs.get("url", [None])[0]
    if inner:
        return canonical_image_key(inner)
    path = parsed.path.lower()
    path = re.sub(r"/(240|320|480|624|660|800|1024|1200|1600)(/|$)", "/SIZE/", path)
    path = re.sub(r"/ic/\d+x\d+/", "/ic/SIZE/", path)
    path = re.sub(r"/resize/[^/]+/", "/resize/SIZE/", path)
    return f"{parsed.netloc.lower()}{path}"


def fetch_text(url, timeout=3):
    headers = dict(HEADERS)
    headers["Accept-Encoding"] = "identity"  # disable compression for text fetches
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def fetch_bytes(url, timeout=8):
    headers = dict(HEADERS)
    if "guim.co.uk" in url or "theguardian.com" in url:
        headers["Referer"] = "https://www.theguardian.com/"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", "application/octet-stream")


def cleanup_cache(cache, max_age):
    now = time.time()
    for k in list(cache.keys()):
        if now - cache[k]["time"] > max_age:
            cache.pop(k, None)


def cleanup_proxy_cache():
    cleanup_cache(PROXY_CACHE, PROXY_CACHE_SECONDS)
    cleanup_cache(REJECT_CACHE, REJECT_CACHE_SECONDS)
    if len(PROXY_CACHE) > PROXY_CACHE_MAX_ITEMS:
        oldest = sorted(PROXY_CACHE.items(), key=lambda kv: kv[1]["time"])
        for key, _ in oldest[:len(PROXY_CACHE) - PROXY_CACHE_MAX_ITEMS]:
            PROXY_CACHE.pop(key, None)


def normalize_link(href, base_url):
    if not href:
        return None
    href = href.strip().replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        parsed = urllib.parse.urlparse(base_url)
        href = f"{parsed.scheme}://{parsed.netloc}{href}"
    if not href.startswith("http"):
        return None
    return href.split("#")[0]


def source_domains_match(url, base_url):
    try:
        u = urllib.parse.urlparse(url).netloc.replace("www.", "")
        b = urllib.parse.urlparse(base_url).netloc.replace("www.", "")
        return bool(u and b and (u == b or u.endswith("." + b) or b.endswith("." + u)))
    except Exception:
        return False


def extract_article_links_from_html(html, base_url, max_links=40):
    links = []
    seen = set()
    for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        link = normalize_link(href, base_url)
        if not link or link in seen:
            continue
        if not source_domains_match(link, base_url):
            continue
        lower = link.lower()
        if any(skip in lower for skip in ["/video/", "/podcast", "/live", "signin", "subscribe", "login"]):
            continue
        seen.add(link)
        links.append(link)
        if len(links) >= max_links:
            break
    return links


def extract_inline_images_from_html(html, base_url, max_images=35):
    imgs = []
    seen = set()
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            img = normalize_link(m.group(1), base_url)
            img = clean_extracted_image_url(img)
            if not img:
                continue
            key = canonical_image_key(img)
            if key in seen:
                continue
            lower = img.lower()
            if not any(token in lower for token in ["ichef.bbci", "cloudfront", "reuters", "guardian", "npr", "brightspotcdn"]):
                continue
            seen.add(key)
            imgs.append(img)
            if len(imgs) >= max_images:
                return imgs
    return imgs


def extract_rss_item_image(item):
    media_ns = {"media": "http://search.yahoo.com/mrss/"}
    content_ns = {"content": "http://purl.org/rss/1.0/modules/content/"}

    thumb = item.find("media:thumbnail", media_ns)
    if thumb is not None:
        url = thumb.attrib.get("url")
        if url:
            # Upgrade Guardian thumbnail to full size.
            url = re.sub(r'width=\d+', 'width=1200', url)
            return clean_extracted_image_url(url)

    for media_content in item.findall("media:content", media_ns):
        url = media_content.attrib.get("url")
        mime = media_content.attrib.get("type", "")
        medium = media_content.attrib.get("medium", "")
        if url and (mime.startswith("image/") or medium == "image"):
            url = re.sub(r'width=\d+', 'width=1200', url)
            return clean_extracted_image_url(url)

    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url", "")
        mime = enclosure.attrib.get("type", "")
        if url and mime.startswith("image/"):
            return clean_extracted_image_url(url)

    # Guardian uses content:encoded with embedded img tags.
    for tag in ["content:encoded", "description"]:
        el = item.find(tag, content_ns) or item.find(tag)
        if el is not None and el.text:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', el.text, re.IGNORECASE)
            if m:
                cleaned = clean_extracted_image_url(m.group(1))
                if cleaned:
                    return cleaned

    # Last resort: search raw XML of the item for any image URL.
    try:
        import xml.etree.ElementTree as _ET
        raw = _ET.tostring(item, encoding="unicode")
        for m in re.finditer(r'https://[^\s\'"<>&]+\.(?:jpg|jpeg|webp)', raw, re.IGNORECASE):
            url = m.group(0).rstrip('.,;)')
            url = re.sub(r'width=\d+', 'width=1200', url)
            cleaned = clean_extracted_image_url(url)
            if cleaned:
                return cleaned
    except Exception:
        pass

    return None



def normalize_image_url_for_dedupe(url):
    """Return a stable dedupe key so resized AP/Reuters URLs do not repeat."""
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    # AP dims URLs put the real asset URL inside ?url=...
    if "url" in query and query["url"]:
        return query["url"][0].split("?")[0]

    # Drop common resize/cache query strings.
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def absolutize_url(url, base_url):
    if not url:
        return ""

    url = url.strip().replace("&amp;", "&")

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        parsed = urllib.parse.urlparse(base_url)
        return f"{parsed.scheme}://{parsed.netloc}{url}"

    return url


def html_unescape_js_urls(text):
    """Make image URLs embedded inside AP/Next JSON easier to regex."""
    if not text:
        return ""
    return (
        text
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
        .replace("%3A", ":")
        .replace("%2F", "/")
    )


def extract_image_urls_from_html(html, base_url, limit=80):
    found = []
    seen = set()

    def add_raw(raw):
        if not raw:
            return False
        # srcset: take the largest-ish candidate, usually last.
        if "," in raw and (" " in raw):
            parts = [p.strip().split(" ")[0] for p in raw.split(",") if p.strip()]
            raw = parts[-1] if parts else raw

        img = absolutize_url(raw, base_url)
        img = clean_extracted_image_url(img)
        if not img:
            return False

        lower = img.lower()
        if any(bad in lower for bad in ["logo", "icon", "avatar", "placeholder", "sprite", "tracking", "favicon"]):
            return False

        if not any(good in lower for good in [
            "ichef.bbci.co.uk",
            "static.reuters.com",
            "cloudfront-us-east-2.images.arcpublishing.com",
            "media.guim.co.uk",
            "i.guim.co.uk",
            "npr.brightspotcdn.com",
            "img.aljazeera.net",
            "www.aljazeera.com/wp-content",
            ".jpg",
            ".jpeg",
            ".webp",
        ]):
            return False
        # media.npr.org is NPR's placeholder/logo domain — real NPR photos use brightspotcdn.
        if "media.npr.org" in lower:
            return False
        # Guardian author avatars and small images.
        if "yimg.com" in lower and (";w=80;" in lower or ";h=60;" in lower or "logo" in lower):
            return False
        if "static.theguardian.com" in lower:
            return False
        if "interactive.guim.co.uk" in lower:
            return False
        # i.guim.co.uk: only block staff photos and avatars.
        if "i.guim.co.uk" in lower:
            if "/img/uploads/" in lower or "/img/static/" in lower:
                return False
        # Guardian composite/collage images — always divided layouts.
        if "guim.co.uk" in lower and "_5000_4000" in lower:
            return False
        # NPR brightspotcdn URLs with non-news filenames (games, puzzles, podcasts etc).
        if "brightspotcdn" in lower and any(bad in lower for bad in [
            "games-we-love", "podcast", "music", "puzzle", "quiz", "crossword",
            "default-wide", "placeholder", "share-image", "shareimage",
        ]):
            return False

        key = normalize_image_url_for_dedupe(img)
        if not key or key in seen:
            return False
        seen.add(key)
        found.append(img)
        return True

    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
        r'<source[^>]+srcset=["\']([^"\']+)["\']',
        r'<img[^>]+srcset=["\']([^"\']+)["\']',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            add_raw(m.group(1))
            if len(found) >= limit:
                return found[:limit]

    expanded = html_unescape_js_urls(html)
    url_patterns = [
        r'https://cloudfront-us-east-2\.images\.arcpublishing\.com/[^"\'\s<>]+',
        r'https://media\.guim\.co\.uk/[^"\'\s<>]+',
        r'https://npr\.brightspotcdn\.com/[^"\'\s<>]+',
    ]
    for pattern in url_patterns:
        for m in re.finditer(pattern, expanded, re.IGNORECASE):
            raw = m.group(0).rstrip('.,;)}]')
            add_raw(raw)
            if len(found) >= limit:
                return found[:limit]

    return found[:limit]

def _scrape_one_page(page_url):
    """Fetch a section page and return (section_images, article_links)."""
    try:
        html = fetch_text(page_url, timeout=3.0)
        imgs = extract_image_urls_from_html(html, page_url, limit=55)
        links = extract_article_links_from_html(html, page_url, max_links=22)
        return imgs, links
    except Exception:
        return [], []


def _scrape_one_article(args):
    """Fetch a single article page and return its image URLs."""
    link, _ = args
    try:
        html = fetch_text(link, timeout=2.4)
        imgs = extract_image_urls_from_html(html, link, limit=5)
        return imgs[:3]
    except Exception:
        return []


def fetch_wikimedia_news_images(limit=60):
    """Fetch documentary news images from Wikimedia Commons 'In the news' category."""
    images = []
    try:
        # Fetch recent "In the news" featured images via the Commons API.
        api_url = (
            "https://commons.wikimedia.org/w/api.php"
            "?action=query&list=categorymembers&cmtitle=Category:Quality_images_of_people"
            "&cmtype=file&cmlimit=50&cmnamespace=6"
            "&prop=imageinfo&iiprop=url|size&iiurlwidth=1200"
            "&format=json&origin=*"
        )
        data = fetch_text(api_url, timeout=6)
        blob = json.loads(data)
        pages = blob.get("query", {}).get("categorymembers", [])
        # Also fetch current events images
        api_url2 = (
            "https://commons.wikimedia.org/w/api.php"
            "?action=query&list=categorymembers&cmtitle=Category:Images_from_Wiki_Loves_Earth_2024"
            "&cmtype=file&cmlimit=30&cmnamespace=6"
            "&format=json&origin=*"
        )
        # Use a simpler approach — query the Portal:Current_events images
        portal_url = (
            "https://en.wikipedia.org/w/api.php"
            "?action=query&titles=Portal:Current_events"
            "&prop=images&imlimit=50&format=json"
        )
        portal_data = fetch_text(portal_url, timeout=6)
        portal_blob = json.loads(portal_data)
        portal_pages = list(portal_blob.get("query", {}).get("pages", {}).values())
        filenames = []
        for page in portal_pages:
            for img in page.get("images", []):
                title = img.get("title", "")
                if title.startswith("File:") and any(
                    title.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".webp"]
                ):
                    filenames.append(title)

        if filenames:
            titles_param = "|".join(filenames[:30])
            info_url = (
                f"https://en.wikipedia.org/w/api.php"
                f"?action=query&titles={urllib.parse.quote(titles_param)}"
                f"&prop=imageinfo&iiprop=url|size&iiurlwidth=1200&format=json"
            )
            info_data = fetch_text(info_url, timeout=6)
            info_blob = json.loads(info_data)
            for page in info_blob.get("query", {}).get("pages", {}).values():
                for ii in page.get("imageinfo", []):
                    url = ii.get("thumburl") or ii.get("url", "")
                    w = ii.get("thumbwidth", 0) or ii.get("width", 0)
                    h = ii.get("thumbheight", 0) or ii.get("height", 0)
                    if url and w >= 800 and h >= 450 and w > h:
                        cleaned = clean_extracted_image_url(url)
                        if cleaned and not url_is_known_bad(cleaned):
                            images.append(cleaned)
                            if len(images) >= limit:
                                return images
    except Exception as e:
        print("[Wikimedia] error:", e)
    return images


def get_direct_page_images(limit=560):
    images = []
    seen = set()

    def add_candidate(img):
        if not img:
            return False
        img = clean_extracted_image_url(img)
        if not img or url_is_known_bad(img):
            return False
        key = normalize_image_url_for_dedupe(img)
        if not key or key in seen:
            return False
        seen.add(key)
        images.append(img)
        return True

    pages = DIRECT_IMAGE_PAGES[:]
    random.shuffle(pages)

    # Phase 1: fetch all section pages in parallel.
    article_links = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_scrape_one_page, p): p for p in pages}
        for fut in as_completed(futures):
            try:
                imgs, links = fut.result()
                random.shuffle(imgs)
                for img in imgs:
                    add_candidate(img)
                for link in links:
                    article_links.append((link, False))
            except Exception:
                pass

    if len(images) >= limit:
        return images[:limit]

    # Phase 2: scrape article pages in parallel.
    random.shuffle(article_links)
    combined = article_links[:80]

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_scrape_one_article, args) for args in combined]
        for fut in as_completed(futures):
            if len(images) >= limit:
                break
            try:
                for img in fut.result():
                    add_candidate(img)
            except Exception:
                pass

    return images[:limit]

def extract_image_from_html_page(url):
    try:
        html = fetch_text(url, timeout=2.5)
        imgs = extract_inline_images_from_html(html, url, max_images=1)
        return imgs[0] if imgs else None
    except Exception:
        return None


def is_bbc_feed_url(url):
    return "bbci.co.uk" in url or "bbc.co.uk" in url


def source_category(url):
    lower = (url or "").lower()
    if "reuters" in lower or "arcpublishing.com" in lower:
        return "reuters"
    if "guim.co.uk" in lower or "theguardian" in lower:
        return "guardian"
    if "npr" in lower or "brightspotcdn" in lower:
        return "npr"
    if "aljazeera" in lower:
        return "other"
    if "bbci.co.uk" in lower or "bbc.co.uk" in lower:
        return "bbc"
    return "other"


def weighted_image_mix(images, limit=MAX_IMAGE_POOL):
    """Favor Guardian; NPR and BBC as secondary."""
    buckets = {"guardian": [], "npr": [], "bbc": [], "other": []}
    for img in images:
        buckets.setdefault(source_category(img), []).append(img)

    for bucket in buckets.values():
        random.shuffle(bucket)

    mixed = (
        buckets["guardian"][:600]
        + buckets["npr"][:150]
        + buckets["other"][:80]
        + buckets["bbc"][:120]
    )

    bbc_in_mixed = sum(1 for i in mixed if source_category(i) == "bbc")
    remaining = []
    already = set(canonical_image_key(i) for i in mixed)
    for name in ["guardian", "npr", "other"]:
        for img in buckets[name]:
            key = canonical_image_key(img)
            if key not in already:
                already.add(key)
                remaining.append(img)
    # Add BBC last, hard-capped at 60 total across both passes.
    bbc_remaining_budget = max(0, 60 - bbc_in_mixed)
    bbc_added = 0
    for img in buckets["bbc"]:
        if bbc_added >= bbc_remaining_budget:
            break
        key = canonical_image_key(img)
        if key not in already:
            already.add(key)
            remaining.append(img)
            bbc_added += 1
    random.shuffle(remaining)
    mixed.extend(remaining)
    return mixed[:limit]


def get_bbc_images(limit=MAX_IMAGE_POOL):
    now = time.time()
    with IMAGE_CACHE["lock"]:
        if IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
            cached = IMAGE_CACHE["images"][:]
            random.shuffle(cached)
            return cached[:limit]

    images = []
    seen = set()
    non_bbc_article_scrape_budget = 60
    bbc_added = 0
    page_article_scrape_budget = 50
    max_bbc_images = int(limit * 0.15)

    _add_image_stats = {"total": 0, "clean_fail": 0, "bad": 0, "dup": 0, "reject_cache": 0, "added": 0}

    def add_image(img):
        _add_image_stats["total"] += 1
        if not img:
            _add_image_stats["clean_fail"] += 1
            return False
        img = clean_extracted_image_url(img)
        if not img:
            _add_image_stats["clean_fail"] += 1
            return False
        if url_is_known_bad(img):
            _add_image_stats["bad"] += 1
            return False
        key = canonical_image_key(img)
        if not key or key in seen:
            _add_image_stats["dup"] += 1
            return False
        rejected = REJECT_CACHE.get(img)
        if rejected and now - rejected["time"] < REJECT_CACHE_SECONDS:
            _add_image_stats["reject_cache"] += 1
            return False
        seen.add(key)
        images.append(img)
        _add_image_stats["added"] += 1
        return True

    # Direct public pages first. This is the most reliable way to get AP scene images.
    for img in get_direct_page_images(limit=1000):
        if len(images) >= limit:
            break
        add_image(img)

    # Fetch Guardian API and RSS feeds in parallel.
    # Guardian API — disabled via flag until rate limit ban lifts.
    for img in get_guardian_api_images():
        add_image(img)

    def fetch_one_feed(feed_url):
        """Fetch one RSS feed and return list of image URLs found."""
        found = []
        try:
            rss = fetch_text(feed_url, timeout=4.0)
            if not rss or len(rss) < 100:
                print(f"[RSS] {feed_url} — empty response", flush=True)
                return found
            root = ET.fromstring(rss)
            items = root.findall(".//item")
            is_bbc = is_bbc_feed_url(feed_url)
            is_guardian_feed = "theguardian.com" in feed_url
            item_limit = 750 if is_bbc else 90
            found_imgs = 0
            for item in items[:item_limit]:
                img = extract_rss_item_image(item)
                if img:
                    cleaned = clean_extracted_image_url(img)
                    if cleaned and not url_is_known_bad(cleaned):
                        found.append((cleaned, is_bbc))
                        found_imgs += 1
                    elif is_guardian_feed and not cleaned:
                        print(f"[RSS] Guardian image rejected by clean: {img[:80]}", flush=True)
                elif not is_bbc:
                    link = item.find("link")
                    if link is not None and link.text:
                        found.append((link.text.strip(), "article_link"))
            name = feed_url.split("/")[-2] if feed_url.endswith("rss") else feed_url.split("/")[-1]
            print(f"[RSS] {name} — {len(items)} items, {found_imgs} images", flush=True)
        except Exception as e:
            print(f"[RSS] {feed_url} error: {e}", flush=True)
        return found

    feeds = RSS_FEEDS[:]
    print(f"[BG] Fetching {len(feeds)} RSS feeds …", flush=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        feed_futures = {ex.submit(fetch_one_feed, f): f for f in feeds}
        article_links_to_scrape = []
        # Collect all results, process non-BBC first to fill pool before BBC cap.
        results = []
        for fut in as_completed(feed_futures):
            try:
                results.append((feed_futures[fut], fut.result()))
            except Exception:
                pass
        # Sort: non-BBC feeds first.
        results.sort(key=lambda x: 1 if is_bbc_feed_url(x[0]) else 0)
        for feed_url, items in results:
            for item in items:
                url_or_link, kind = item
                if kind == "article_link":
                    article_links_to_scrape.append(url_or_link)
                else:
                    is_bbc = kind
                    if is_bbc and bbc_added >= max_bbc_images:
                        continue
                    if add_image(url_or_link) and is_bbc:
                        bbc_added += 1

    print(f"[BG] After RSS feeds: {len(images)} images", flush=True)
    print(f"[BG] add_image stats: {_add_image_stats}", flush=True)

    # Scrape article pages from RSS that had no inline image, in parallel.
    random.shuffle(article_links_to_scrape)
    with ThreadPoolExecutor(max_workers=6) as ex:
        article_futures = [ex.submit(extract_image_from_html_page, l)
                          for l in article_links_to_scrape[:non_bbc_article_scrape_budget]]
        for fut in as_completed(article_futures):
            try:
                add_image(fut.result())
            except Exception:
                pass

    def scrape_source_page(page_url):
        found_imgs = []
        found_links = []
        try:
            html = fetch_text(page_url, timeout=3.0)
            found_imgs = extract_inline_images_from_html(html, page_url, max_images=60)
            found_links = extract_article_links_from_html(html, page_url, max_links=60)
        except Exception:
            pass
        return found_imgs, found_links

    pages = SOURCE_PAGES[:]
    random.shuffle(pages)
    all_article_links = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        page_futures = {ex.submit(scrape_source_page, p): p for p in pages}
        for fut in as_completed(page_futures):
            try:
                imgs, links = fut.result()
                for img in imgs:
                    add_image(img)
                all_article_links.extend(links)
            except Exception:
                pass

    random.shuffle(all_article_links)
    with ThreadPoolExecutor(max_workers=6) as ex:
        art_futures = [ex.submit(extract_image_from_html_page, l)
                      for l in all_article_links[:page_article_scrape_budget]]
        for fut in as_completed(art_futures):
            try:
                add_image(fut.result())
            except Exception:
                pass

    guardian_imgs = [img for img in images if source_category(img) == "guardian"]
    npr_imgs = [img for img in images if source_category(img) == "npr"]
    other_imgs = [img for img in images if source_category(img) not in ("guardian", "npr", "bbc")]
    bbc_imgs = [img for img in images if source_category(img) == "bbc"]

    ordered = guardian_imgs + npr_imgs + other_imgs + bbc_imgs

    with IMAGE_CACHE["lock"]:
        IMAGE_CACHE["time"] = now
        IMAGE_CACHE["images"] = ordered[:]
    return ordered[:limit]


def _background_pool_refresher():
    """Continuously rebuild the image pool so the cache is always warm."""
    refresh_count = 0
    while True:
        try:
            print("[BG] Refreshing image pool …", flush=True)
            t0 = time.time()
            if refresh_count % 10 == 0:
                APPROVED_URLS.clear()
                print("[BG] Cleared approved URLs for fresh vet", flush=True)
            refresh_count += 1
            images = get_bbc_images(limit=MAX_IMAGE_POOL)
            print(f"[BG] Pool ready: {len(images)} images in {time.time()-t0:.1f}s", flush=True)
            _pre_cache_seed(images)
            _pre_vet_pool(images)
        except Exception as e:
            import traceback
            print("[BG] Refresh error:", e, flush=True)
            traceback.print_exc()
        time.sleep(BACKGROUND_REFRESH_SECONDS)


def _pre_cache_seed(images):
    """Pre-fetch and cache the first few Guardian images so they serve instantly on page load."""
    seed = [img for img in images
            if "guim.co.uk" in img
            and not url_is_known_bad(img)][:20]
    print(f"[BG] Pre-caching {len(seed)} seed images …", flush=True)
    cached_count = 0
    for url in seed:
        if url in PROXY_CACHE:
            cached_count += 1
            continue
        try:
            data, content_type = fetch_bytes(url, timeout=8)
            if content_type.startswith("image/") and len(data) > 10000:
                PROXY_CACHE[url] = {"time": time.time(), "data": data, "content_type": content_type}
                cached_count += 1
        except Exception as e:
            print(f"[SEED] fetch failed: {e}", flush=True)
    print(f"[BG] Seed pre-cached: {cached_count}/{len(seed)} succeeded.", flush=True)


def _pre_vet_one(url):
    """Fetch and check one image; add to APPROVED_URLS if it passes."""
    if url in APPROVED_URLS:
        return
    # Non-Guardian, non-BBC sources — approve without cv2.
    is_guardian = "guim.co.uk" in url or "theguardian.com" in url
    is_bbc = "bbci.co.uk" in url or "bbc.co.uk" in url
    if not is_guardian and not is_bbc:
        APPROVED_URLS.add(url)
        return
    # Guardian and BBC — run cv2 checks.
    try:
        data, content_type = fetch_bytes(url, timeout=8)
        if not content_type.startswith("image/"):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            REJECT_CACHE[url] = {"time": time.time()}
            return
        ih, iw = img.shape[:2]
        min_w = 1000 if "brightspotcdn" in url else MIN_IMAGE_WIDTH
        min_h = 600 if "brightspotcdn" in url else MIN_IMAGE_HEIGHT
        if iw < min_w or ih < min_h:
            REJECT_CACHE[url] = {"time": time.time()}
            return
        if ih > iw * 1.4:
            REJECT_CACHE[url] = {"time": time.time()}
            return
        if image_is_portrait_or_generic_isolated_subject(data):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        if image_is_probably_full_graphic_page(data):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        if image_has_center_divider(data):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        APPROVED_URLS.add(url)
    except Exception:
        pass


def _pre_vet_pool(images):
    """Pre-vet all images in the pool in the background using a thread pool."""
    REJECT_CACHE.clear()
    to_vet = [u for u in images if u not in APPROVED_URLS]
    print(f"[BG] Pre-vetting {len(to_vet)} images …", flush=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_pre_vet_one, to_vet))
    print(f"[BG] Pre-vet done. Approved: {len(APPROVED_URLS)}", flush=True)


def image_is_probably_full_graphic_page(data):
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False
    if img is None or img.size == 0:
        return False
    h, w = img.shape[:2]
    if max(w, h) > 900:
        scale = 900.0 / max(w, h)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray_std = float(np.std(gray))
    mean_brightness = float(np.mean(gray))
    edges = cv2.Canny(gray, 70, 170)
    edge_density = float(np.mean(edges > 0))
    small = cv2.resize(img, (100, max(56, int(img.shape[0] * 100 / img.shape[1]))), interpolation=cv2.INTER_AREA)
    unique_colors = len(np.unique(small.reshape(-1, 3), axis=0))
    blue_mask = (hsv[:, :, 0] > 98) & (hsv[:, :, 0] < 138) & (hsv[:, :, 1] > 80) & (hsv[:, :, 2] > 45)
    green_mask = (hsv[:, :, 0] > 42) & (hsv[:, :, 0] < 90) & (hsv[:, :, 1] > 85) & (hsv[:, :, 2] > 55)
    white_mask = gray > 215
    blue_frac = float(np.mean(blue_mask))
    green_frac = float(np.mean(green_mask))
    white_frac = float(np.mean(white_mask))
    if blue_frac > 0.22 and white_frac > 0.025:
        return True
    if green_frac > 0.018 and white_frac > 0.025 and edge_density > 0.055:
        return True
    if blue_frac > 0.14 and green_frac > 0.006 and white_frac > 0.018:
        return True
    if unique_colors < 1100 and edge_density > 0.075 and gray_std < 62:
        return True
    if unique_colors < 750 and mean_brightness > 65:
        return True
    if gray_std < 36 and edge_density > 0.07:
        return True
    return False


def top_has_bbc_branding(img):
    if img is None or img.size == 0:
        return False
    try:
        h, w = img.shape[:2]
        top = img[:max(1, int(h * 0.42)), :]
        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
        blue_mask = (hsv[:, :, 0] > 98) & (hsv[:, :, 0] < 138) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 40)
        green_mask = (hsv[:, :, 0] > 42) & (hsv[:, :, 0] < 92) & (hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 50)
        white_mask = gray > 210
        blue_frac = float(np.mean(blue_mask))
        green_frac = float(np.mean(green_mask))
        white_frac = float(np.mean(white_mask))
        edges = cv2.Canny(gray, 55, 150)
        edge_density = float(np.mean(edges > 0))
        gray_std = float(np.std(gray))
        if blue_frac > 0.025 and white_frac > 0.018:
            return True
        if green_frac > 0.0025 and white_frac > 0.016:
            return True
        if edge_density > 0.095 and gray_std < 86 and white_frac > 0.012:
            return True
        return False
    except Exception:
        return False


def crop_top_if_needed(img, url=""):
    if img is None or img.size == 0:
        return img, False
    try:
        h, w = img.shape[:2]
        if url_needs_voice_crop(url):
            cropped = img[int(h * 0.11):, :]
            return (cropped, True) if cropped is not None and cropped.size > 0 else (img, False)
        if not top_has_bbc_branding(img):
            return img, False
        cropped = img[int(h * 0.18):, :]
        return (cropped, True) if cropped is not None and cropped.size > 0 else (img, False)
    except Exception:
        return img, False



def get_cv2_face_cascade():
    """Return OpenCV's bundled frontal-face cascade if available."""
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        cascade = cv2.CascadeClassifier(cascade_path)
        if cascade.empty():
            return None
        return cascade
    except Exception:
        return None


def image_is_portrait_or_generic_isolated_subject(data):
    """
    Reject images that read like a single cut-out/portrait rather than a news scene.

    This catches:
    - centered single faces / headshots
    - cropped isolated people/objects on smooth generic backgrounds
    - vertical/cropped editorial photos that survive URL rules
    It tries not to reject crowds, landscapes, street scenes, or busy interiors.
    """
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]
    if w < 120 or h < 120:
        return False

    # No verticals / phone crops in the main pool.
    if h > w * 1.05:
        return True

    # Work at a stable analysis size.
    target_w = 640
    if w > target_w:
        scale = target_w / float(w)
        img = cv2.resize(img, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)

    # 0) Square-ish crops with high skin content are almost always portraits.
    if 0.7 < (h / float(w)) < 1.4:
        pass  # continue to other checks
    
    # 1) Face/headshot rejection. Only reject when a face clearly dominates the frame.
    cascade = get_cv2_face_cascade()
    if cascade is not None:
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.08,
            minNeighbors=4,
            minSize=(max(30, int(w * 0.05)), max(30, int(h * 0.06))),
        )
        if len(faces) == 1:
            x, y, fw, fh = faces[0]
            face_area = (fw * fh) / float(w * h)
            cx = (x + fw / 2) / float(w)
            cy = (y + fh / 2) / float(h)
            centered = 0.20 < cx < 0.80 and 0.08 < cy < 0.65
            # Reject if face takes up >2% of frame (lowered from 4%)
            if centered and face_area > 0.02:
                return True
        elif len(faces) == 2:
            total_area = sum((fw * fh) for (x, y, fw, fh) in faces) / float(w * h)
            if total_area > 0.04:
                return True
        elif len(faces) >= 3:
            total_area = sum((fw * fh) for (x, y, fw, fh) in faces) / float(w * h)
            if total_area > 0.08:
                return True

    # 2) Generic background / isolated subject rejection.
    edges = cv2.Canny(gray, 60, 155)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    # Border is a proxy for plain studio/wall/sky/background.
    border_mask = np.zeros((h, w), dtype=bool)
    b_y = max(10, int(h * 0.13))
    b_x = max(10, int(w * 0.10))
    border_mask[:b_y, :] = True
    border_mask[-b_y:, :] = True
    border_mask[:, :b_x] = True
    border_mask[:, -b_x:] = True

    center_mask = np.zeros((h, w), dtype=bool)
    center_mask[int(h * 0.16):int(h * 0.88), int(w * 0.22):int(w * 0.78)] = True

    border_edge = float(np.mean(edges[border_mask] > 0))
    center_edge = float(np.mean(edges[center_mask] > 0))
    border_std = float(np.std(gray[border_mask]))
    border_sat_std = float(np.std(sat[border_mask]))
    border_sat_mean = float(np.mean(sat[border_mask]))
    border_val_mean = float(np.mean(val[border_mask]))

    # Count color variety in the border. Generic backgrounds have low variety.
    sample = cv2.resize(img, (120, max(68, int(h * 120 / w))), interpolation=cv2.INTER_AREA)
    sh, sw = sample.shape[:2]
    smask = np.zeros((sh, sw), dtype=bool)
    sy = max(5, int(sh * 0.13))
    sx = max(5, int(sw * 0.10))
    smask[:sy, :] = True
    smask[-sy:, :] = True
    smask[:, :sx] = True
    smask[:, -sx:] = True
    quant = (sample // 24).astype(np.uint8)
    border_unique = len(np.unique(quant[smask].reshape(-1, 3), axis=0))

    plain_background = (
        (border_edge < 0.055 and border_std < 60 and border_unique < 130)
        or (border_edge < 0.040 and border_sat_std < 45 and border_unique < 110)
        or (border_edge < 0.030 and border_sat_mean < 75 and border_val_mean > 80)
    )
    isolated_subject = center_edge > max(0.040, border_edge * 1.7)

    if plain_background and isolated_subject:
        return True

    # 3) Reject obvious single-person waist-up crops even if the face detector misses.
    y0, y1 = int(h * 0.08), int(h * 0.78)
    x0, x1 = int(w * 0.20), int(w * 0.80)
    crop_hsv = hsv[y0:y1, x0:x1, :]
    if crop_hsv.size:
        hue = crop_hsv[:, :, 0]
        sat_c = crop_hsv[:, :, 1]
        val_c = crop_hsv[:, :, 2]
        skinish = ((hue < 24) | (hue > 165)) & (sat_c > 35) & (sat_c < 185) & (val_c > 55)
        skinish_frac = float(np.mean(skinish))
        if skinish_frac > 0.055 and border_edge < 0.055 and border_unique < 160:
            return True

    return False

def image_has_center_divider(data):
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False
    if img is None or img.size == 0:
        return False
    h, w = img.shape[:2]
    if w < 120 or h < 120:
        return False
    target_w = 520
    if w > target_w:
        scale = target_w / float(w)
        img = cv2.resize(img, (target_w, int(h * scale)), interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    bright = gray > 218
    dark = gray < 38
    center_min = int(w * 0.18)
    center_max = int(w * 0.82)
    for x in range(center_min, center_max):
        bright_band = bright[:, max(0, x - 1):min(w, x + 2)]
        dark_band = dark[:, max(0, x - 1):min(w, x + 2)]
        bright_by_row = np.mean(bright_band, axis=1) > 0.45
        dark_by_row = np.mean(dark_band, axis=1) > 0.45
        for line_by_row in (bright_by_row, dark_by_row):
            full_height_frac = float(np.mean(line_by_row))
            if full_height_frac > 0.58:
                return True
            if full_height_frac > 0.42:
                transitions = np.diff(line_by_row.astype(np.int8))
                if int(np.sum(transitions == 1)) <= 8:
                    return True
    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    edge_strength = np.abs(grad_x)
    col_energy = edge_strength.mean(axis=0)
    center_energy = col_energy[center_min:center_max]
    if center_energy.size == 0:
        return False
    baseline = float(np.median(col_energy)) + 1e-6
    # Count how many columns have strong vertical edge energy — multiple dividers
    # show up as multiple high-energy columns across the image.
    strong_cols = np.sum(center_energy > baseline * 2.2)
    if strong_cols >= 2:
        # Check each strong column spans most of the image height.
        strong_col_indices = np.where(center_energy > baseline * 2.2)[0]
        for ci in strong_col_indices:
            abs_ci = center_min + ci
            col_slice = edge_strength[:, max(0, abs_ci - 1):min(w, abs_ci + 2)]
            row_strength = col_slice.mean(axis=1)
            row_baseline = float(np.median(row_strength)) + 1e-6
            strong_frac = float(np.mean(row_strength > row_baseline * 1.55))
            if strong_frac > 0.38:
                return True
    # Single divider check — original logic.
    divider_x = center_min + int(np.argmax(center_energy))
    peak_energy = float(col_energy[divider_x])
    if peak_energy < baseline * 2.2:
        return False
    col_slice = edge_strength[:, max(0, divider_x - 1):min(w, divider_x + 2)]
    row_strength = col_slice.mean(axis=1)
    row_baseline = float(np.median(row_strength)) + 1e-6
    strong_frac = float(np.mean(row_strength > row_baseline * 1.55))
    return strong_frac > 0.38


def render_html():
    with IMAGE_CACHE["lock"]:
        cached = IMAGE_CACHE["images"][:]
    # Seed with Guardian images first — include approved and pending (not rejected).
    seed = [img for img in cached
            if "guim.co.uk" in img
            and not url_is_known_bad(img)
            and img not in REJECT_CACHE][:10]
    if len(seed) < 5:
        seed += [img for img in cached
                 if "brightspotcdn" in img
                 and not url_is_known_bad(img)
                 and img not in REJECT_CACHE][:10 - len(seed)]
    sequence = []
    for img in seed:
        src = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({"src": src, "raw": img, "verticalOnly": url_is_vertical_only(img)})
    sequence_json = json.dumps(sequence).replace('\n', '\\n').replace('\r', '')
    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>misshurry</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="misshurry">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#000000">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png">
<style>
html, body {{ margin:0; padding:0; width:100%; height:100%; overflow:hidden; background:#000; cursor:none; }}
canvas {{ display:block; width:100vw; height:100vh; touch-action:none; }}
#debug-url {{
  display: block;
  position: fixed;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  color: rgba(255,255,255,0.55);
  font: 11px/1.4 monospace;
  text-align: center;
  max-width: 90vw;
  word-break: break-all;
  cursor: pointer;
  z-index: 9999;
  padding: 4px 8px;
  background: rgba(0,0,0,0.35);
  border-radius: 4px;
  transition: color 0.2s;
}}
#debug-url:hover {{ color: rgba(255,255,255,0.9); }}
</style>
</head>
<body>
<div id="debug-url"></div>
<canvas id="view"></canvas>
<script>
if ('serviceWorker' in navigator) {{
  navigator.serviceWorker.register('/sw.js').catch(() => {{}});
}}

// Show install instructions if on mobile browser (not PWA).
let _slideshowReady = false;
function startSlideshow() {{
  if (_slideshowReady) return;
  _slideshowReady = true;
  resizeCanvas(); mouseX=canvas.width/2; mouseY=canvas.height/2; refillPool(); preloadNext();

  (function tryLoad() {{
    if (!currentImage) {{ loadRandomSlide(); setTimeout(tryLoad, 500); }}
  }})();

  // Consistent rotation with preloading.
  const SLIDE_INTERVAL = 8000;
  let _nextPreloaded = null;
  let _nextSrc = null;

  function prepareNextSlide() {{
    const src = getNextRandomSrc();
    if (!src) return;
    _nextSrc = src;
    _nextPreloaded = null;
    const img = new Image();
    img.onload = () => {{ _nextPreloaded = img; }};
    img.onerror = () => {{ badSrcs.add(src); _nextSrc = null; prepareNextSlide(); }};
    img.src = src;
  }}

  function rotateSlides() {{
    if (_nextPreloaded && _nextSrc) {{
      if (!isVerticalPhone() && _nextPreloaded.naturalHeight > _nextPreloaded.naturalWidth * 1.08) {{
        badSrcs.add(_nextSrc);
        _nextSrc = null; _nextPreloaded = null;
        prepareNextSlide();
      }} else {{
        prepareAndDraw(_nextPreloaded, _nextSrc);
        _nextSrc = null; _nextPreloaded = null;
      }}
    }} else {{
      loadRandomSlide();
    }}
    prepareNextSlide();
    setTimeout(rotateSlides, 8000);
  }}

  (function waitForFirst() {{
    if (!currentImage) {{ setTimeout(waitForFirst, 500); return; }}
    prepareNextSlide();
    setTimeout(rotateSlides, SLIDE_INTERVAL);
  }})();

  (function pollImages() {{
    async function refresh() {{
      try {{
        const r = await fetch("/images.json");
        if (r.ok) {{
          const fresh = await r.json();
          if (fresh && fresh.length > 0) {{
            const existingKeys = new Set(slides.map(s => s.src));
            let added = 0;
            for (const item of fresh) {{
              if (!existingKeys.has(item.src)) {{
                slides.push(item); existingKeys.add(item.src); added++;
              }}
            }}
            if (added > 0) {{
              const newSrcs = [];
              for (const item of fresh) {{
                if (!badSrcs.has(item.src) && slideAllowedForCurrentOrientation(item)) newSrcs.push(item.src);
              }}
              if (newSrcs.length > 0) {{
                // Insert new images right after current position so they appear soon.
                const insertAt = Math.min(poolIndex + 1, shuffledPool.length);
                shuffledPool.splice(insertAt, 0, ...shuffleArray(newSrcs));
              }}
              if (!currentImage) loadRandomSlide();
            }}
            setTimeout(refresh, slides.length > 50 ? 15000 : 3000);
          }} else {{ setTimeout(refresh, 2000); }}
        }} else {{ setTimeout(refresh, 2000); }}
      }} catch(e) {{ setTimeout(refresh, 2000); }}
    }}
    setTimeout(refresh, 0);
  }})();
}}

let slides = {sequence_json};
const SEQUENCE_LENGTH_JS = {SEQUENCE_LENGTH};
const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d", {{ willReadFrequently: true }});
let currentPrepared = null, currentImage = null, currentSrc = null;
let mouseX = 0, mouseY = 0, DPR = 1, VIEW_W = window.innerWidth, VIEW_H = window.innerHeight;
let shuffledPool = [], poolIndex = 0, isLoadingSlide = false;
let recentlyShown = [];
const RECENT_LIMIT = 1200;

function syncContextQuality(targetCtx) {{ targetCtx.imageSmoothingEnabled = true; targetCtx.imageSmoothingQuality = "high"; }}
function resizeCanvas() {{
  DPR = Math.max(1, Math.min(window.devicePixelRatio || 1, 2));
  VIEW_W = window.innerWidth; VIEW_H = window.innerHeight;
  canvas.width = Math.round(VIEW_W * DPR); canvas.height = Math.round(VIEW_H * DPR);
  canvas.style.width = VIEW_W + "px"; canvas.style.height = VIEW_H + "px";
  syncContextQuality(ctx);
  if (!mouseX && !mouseY) {{ mouseX = canvas.width / 2; mouseY = canvas.height / 2; }}
}}
function fitCover(sw, sh, dw, dh) {{ const scale = Math.max(dw/sw, dh/sh); const w=sw*scale, h=sh*scale; return {{x:(dw-w)/2, y:(dh-h)/2, w, h}}; }}
function shuffleArray(arr) {{ const a=arr.slice(); for(let i=a.length-1;i>0;i--) {{ const j=Math.floor(Math.random()*(i+1)); [a[i],a[j]]=[a[j],a[i]]; }} return a; }}
function isVerticalPhone() {{ return window.matchMedia("(pointer: coarse)").matches && window.innerHeight > window.innerWidth; }}
function slideAllowedForCurrentOrientation(slide) {{ return !(slide.verticalOnly && !isVerticalPhone()); }}
const badSrcs = new Set();
const _shownThisCycle = new Set();
function sourceScore(src) {{
  if (src.includes('guim.co.uk')) return 0;
  if (src.includes('brightspotcdn')) return 1;
  if (src.includes('bbci.co.uk')) return 2;
  return 3;
}}
function refillPool() {{
  let candidates = slides
    .filter(slideAllowedForCurrentOrientation)
    .map(s => s.src)
    .filter(src => !badSrcs.has(src));

  if (candidates.length < 10 && badSrcs.size > 0) {{
    badSrcs.clear();
    candidates = slides.filter(slideAllowedForCurrentOrientation).map(s => s.src);
  }}

  candidates = [...new Set(candidates)];

  let fresh = candidates.filter(src => !_shownThisCycle.has(src));
  if (fresh.length < 15) {{
    _shownThisCycle.clear();
    fresh = candidates;
  }}

  // AP images first (newest stories), then shuffle Guardian/BBC within their groups.
  const groups = [0, 1, 2, 3].map(score => {{
    const group = fresh.filter(src => sourceScore(src) === score);
    return score === 0 ? group : shuffleArray(group);
  }});
  shuffledPool = groups.flat();
  poolIndex = 0;
}}
function getNextRandomSrc() {{
  if (!shuffledPool.length || poolIndex >= shuffledPool.length) refillPool();
  if (!shuffledPool.length) return null;
  // Skip current image to avoid immediate repeat.
  if (shuffledPool[poolIndex] === currentSrc && shuffledPool.length > 1) poolIndex++;
  if (poolIndex >= shuffledPool.length) refillPool();
  return shuffledPool[poolIndex++];
}}

// Preload cache — keeps next N images ready so transitions are instant.
const preloadCache = new Map(); // src -> Image (loaded)
const PRELOAD_AHEAD = 3;
function preloadNext() {{
  for (let i = 0; i < PRELOAD_AHEAD; i++) {{
    const src = shuffledPool[poolIndex + i];
    if (src && !preloadCache.has(src) && !badSrcs.has(src)) {{
      const img = new Image();
      img.onload = () => preloadCache.set(src, img);
      img.onerror = () => {{ badSrcs.add(src); }};
      preloadCache.set(src, null); // mark as in-flight
      img.src = src;
    }}
  }}
  // Evict old entries to keep memory tidy.
  if (preloadCache.size > 20) {{
    const keys = [...preloadCache.keys()];
    keys.slice(0, keys.length - 20).forEach(k => preloadCache.delete(k));
  }}
}}
function makeImage(sourceImage) {{
  const off = document.createElement("canvas"); off.width = canvas.width; off.height = canvas.height;
  const offCtx = off.getContext("2d", {{ willReadFrequently: true }}); syncContextQuality(offCtx);
  offCtx.fillStyle = "#000"; offCtx.fillRect(0,0,off.width,off.height);
  const fit = fitCover(sourceImage.width, sourceImage.height, off.width, off.height);

  // Process at half resolution then scale up — 4x fewer pixels, imperceptible quality loss.
  const small = document.createElement("canvas");
  small.width = Math.ceil(off.width / 2); small.height = Math.ceil(off.height / 2);
  const smallCtx = small.getContext("2d", {{ willReadFrequently: true }});
  smallCtx.drawImage(sourceImage, 0,0, sourceImage.width, sourceImage.height,
    fit.x/2, fit.y/2, fit.w/2, fit.h/2);
  const imageData = smallCtx.getImageData(0,0,small.width,small.height);
  const data = imageData.data;
  const levels = 28; const step = 255/(levels-1);
  for(let i=0;i<data.length;i+=4) {{
    let gray = 0.299*data[i]+0.587*data[i+1]+0.114*data[i+2];
    gray = Math.round(gray/step)*step;
    data[i]=gray; data[i+1]=gray; data[i+2]=gray; data[i+3]=255;
  }}
  smallCtx.putImageData(imageData,0,0);

  // Scale back up to full canvas.
  offCtx.imageSmoothingEnabled = true;
  offCtx.imageSmoothingQuality = "high";
  offCtx.drawImage(small, 0,0, small.width,small.height, 0,0, off.width,off.height);
  return off;
}}
function drawFallbackMessage() {{ ctx.clearRect(0,0,canvas.width,canvas.height); ctx.fillStyle="#000"; ctx.fillRect(0,0,canvas.width,canvas.height); }}
function drawFlashlight() {{
  if (!currentPrepared) {{ drawFallbackMessage(); return; }}
  const isTouchDevice = window.matchMedia("(pointer: coarse)").matches;
  const minDim = Math.min(canvas.width, canvas.height);
  const radius = minDim * (isTouchDevice ? 0.18 : 0.16);
  ctx.clearRect(0,0,canvas.width,canvas.height); ctx.fillStyle="#000"; ctx.fillRect(0,0,canvas.width,canvas.height);
  const cutout = ctx.createRadialGradient(mouseX,mouseY,0,mouseX,mouseY,radius);
  cutout.addColorStop(0.00,"rgba(255,248,190,1.00)"); cutout.addColorStop(0.20,"rgba(255,238,150,0.84)"); cutout.addColorStop(0.50,"rgba(255,220,95,0.46)"); cutout.addColorStop(0.82,"rgba(255,200,55,0.18)"); cutout.addColorStop(1.00,"rgba(255,185,35,0.00)");
  ctx.globalCompositeOperation="destination-out"; ctx.fillStyle=cutout; ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.globalCompositeOperation="destination-over"; ctx.drawImage(currentPrepared,0,0,canvas.width,canvas.height);
  ctx.globalCompositeOperation="source-over";
  const warm = ctx.createRadialGradient(mouseX,mouseY,0,mouseX,mouseY,radius*1.12);
  warm.addColorStop(0.00,"rgba(255,222,95,0.36)"); warm.addColorStop(0.45,"rgba(255,205,60,0.20)"); warm.addColorStop(0.85,"rgba(255,185,35,0.075)"); warm.addColorStop(1.00,"rgba(255,170,20,0.00)");
  ctx.fillStyle=warm; ctx.fillRect(0,0,canvas.width,canvas.height);
}}
function prepareAndDraw(img, src) {{
  currentImage = img; currentSrc = src; currentPrepared = makeImage(img); drawFlashlight();
  recentlyShown.push(src); if (recentlyShown.length > RECENT_LIMIT) recentlyShown.shift();
  _shownThisCycle.add(src);
  const rawUrl = src.startsWith('/proxy?url=') ? decodeURIComponent(src.replace("/proxy?url=", "")) : src;
  const usableEstimate = Math.max(0, slides.length - badSrcs.size);
  const el = document.getElementById("debug-url");
  el.textContent = rawUrl + "  [recent " + recentlyShown.length + " / usable ~" + usableEstimate + " / bad " + badSrcs.size + "]";
  el.title = "Click to copy image URL"; el.dataset.url = rawUrl;
}}
function loadRandomSlide(attempts=0) {{
  if (isLoadingSlide) return;
  isLoadingSlide = true;

  if (!slides.length || attempts > 400) {{
    isLoadingSlide = false;
    return;
  }}

  const src = getNextRandomSrc();
  if (!src) {{
    isLoadingSlide = false;
    return;
  }}

  const loader = new Image();
  loader.decoding = "async";

  // Safety valve — if the image neither loads nor errors within 6s, move on.
  let timeout = setTimeout(() => {{
    isLoadingSlide = false;
    badSrcs.add(src);
    loadRandomSlide(attempts + 1);
  }}, 6000);

  // Use preloaded image if ready, otherwise fetch normally.
  const preloaded = preloadCache.get(src);
  if (preloaded && preloaded.complete && preloaded.naturalWidth > 0) {{
    preloadCache.delete(src);
    clearTimeout(timeout);
    if (!isVerticalPhone() && preloaded.naturalHeight > preloaded.naturalWidth * 1.08) {{
      badSrcs.add(src);
      isLoadingSlide = false;
      preloadNext();
      setTimeout(() => loadRandomSlide(attempts + 1), 30);
      return;
    }}
    prepareAndDraw(preloaded, src);
    isLoadingSlide = false;
    preloadNext();
    return;
  }}

  loader.onload = () => {{
    clearTimeout(timeout);
    if (!isVerticalPhone() && loader.naturalHeight > loader.naturalWidth * 1.08) {{
      badSrcs.add(src);
      shuffledPool = shuffledPool.filter(s => s !== src);
      isLoadingSlide = false;
      setTimeout(() => loadRandomSlide(attempts + 1), 30);
      return;
    }}
    prepareAndDraw(loader, src);
    isLoadingSlide = false;
    preloadNext();
  }};

  loader.onerror = () => {{
    clearTimeout(timeout);
    badSrcs.add(src);
    shuffledPool = shuffledPool.filter(s => s !== src);
    isLoadingSlide = false;
    setTimeout(() => loadRandomSlide(attempts + 1), 30);
  }};

  loader.src = src;
}}
let _rafPending = false;
function updateFlashlightPositionFromPointer(e) {{ const rect=canvas.getBoundingClientRect(); const isTouchDevice=window.matchMedia("(pointer: coarse)").matches; const offsetY=isTouchDevice ? window.innerHeight*0.12 : 0; mouseX=(e.clientX-rect.left)*DPR; mouseY=((e.clientY-rect.top)-offsetY)*DPR; if (!_rafPending) {{ _rafPending = true; requestAnimationFrame(() => {{ _rafPending = false; drawFlashlight(); }}); }} }}
canvas.addEventListener("pointermove", updateFlashlightPositionFromPointer);
const debugUrlEl = document.getElementById("debug-url");
debugUrlEl.addEventListener("click", async (e) => {{ e.stopPropagation(); const url=debugUrlEl.dataset.url || debugUrlEl.textContent; if(!url) return; try {{ await navigator.clipboard.writeText(url); const oldText=debugUrlEl.textContent; debugUrlEl.textContent="copied"; setTimeout(() => {{ debugUrlEl.textContent=oldText; }}, 650); }} catch(err) {{ window.prompt("Copy image URL:", url); }} }});
window.addEventListener("resize", () => {{ resizeCanvas(); if(currentImage) {{ currentPrepared = makeImage(currentImage); drawFlashlight(); }} else {{ loadRandomSlide(); }} }});

startSlideshow();
</script>
</body>
</html>'''


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def safe_write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def safe_send_bytes(self, status_code, data, content_type="text/plain; charset=utf-8", extra_headers=None):
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store" if content_type.startswith("text/html") else "public, max-age=300")
            if extra_headers:
                for key, value in extra_headers.items():
                    self.send_header(key, value)
            self.end_headers()
            self.safe_write(data)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ["/", "/index.html"]:
            data = render_html().encode("utf-8")
            self.safe_send_bytes(200, data, "text/html; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/images.json":
            with IMAGE_CACHE["lock"]:
                cached = IMAGE_CACHE["images"][:]
            # Always include AP dims URLs immediately — they're auto-approved.
            # Include other sources only once pre-vetting has approved them.
            # If APPROVED_URLS is empty (first boot), return everything so client isn't blank.
            if APPROVED_URLS:
                cached = [img for img in cached
                         if (img in APPROVED_URLS
                              or (img not in REJECT_CACHE
                                  and not url_is_known_bad(img)))]
            sequence = []
            for img in cached:
                src = "/proxy?url=" + urllib.parse.quote(img, safe="")
                sequence.append({"src": src, "raw": img, "verticalOnly": url_is_vertical_only(img)})
            data = json.dumps(sequence).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/manifest.json":
            manifest = json.dumps({
                "name": "misshurry",
                "short_name": "misshurry",
                "description": "news images through a flashlight",
                "start_url": "/",
                "display": "fullscreen",
                "orientation": "any",
                "background_color": "#000000",
                "theme_color": "#000000",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                ]
            }).encode("utf-8")
            self.safe_send_bytes(200, manifest, "application/manifest+json", {"Cache-Control": "public, max-age=3600"})
            return

        if path == "/sw.js":
            sw = (
                "self.addEventListener('install', e => self.skipWaiting());\n"
                "self.addEventListener('activate', e => clients.claim());\n"
                "self.addEventListener('fetch', e => {\n"
                "  if (e.request.url.endsWith('/') || e.request.url.endsWith('/index.html')) {\n"
                "    e.respondWith(fetch(e.request));\n"
                "  }\n"
                "});\n"
            ).encode("utf-8")
            self.safe_send_bytes(200, sw, "application/javascript", {"Cache-Control": "no-cache"})
            return

        if path in ["/icon-192.png", "/icon-512.png"]:
            size = 512 if "512" in path else 192
            # Generate a simple black square with white text as the icon.
            try:
                import struct, zlib
                def make_icon(size):
                    img_data = []
                    for y in range(size):
                        row = [0, 0, 0, 255] * size  # black RGBA
                        img_data.append(bytes([0]) + bytes(row))  # filter byte
                    raw = b"".join(img_data)
                    compressed = zlib.compress(raw, 9)
                    def chunk(name, data):
                        c = name + data
                        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
                    sig = b"\x89PNG\r\n\x1a\n"
                    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                    idat = chunk(b"IDAT", compressed)
                    iend = chunk(b"IEND", b"")
                    return sig + ihdr + idat + iend
                icon_data = make_icon(size)
                self.safe_send_bytes(200, icon_data, "image/png", {"Cache-Control": "public, max-age=86400"})
            except Exception:
                self.safe_send_bytes(404, b"icon not found")
            return

        if path == "/vetted.json":
            with IMAGE_CACHE["lock"]:
                pool = IMAGE_CACHE["images"][:]
            approved = [u for u in pool if u in APPROVED_URLS]
            rejected = [u for u in pool if u in REJECT_CACHE]
            pending = [u for u in pool if u not in APPROVED_URLS and u not in REJECT_CACHE]
            data = json.dumps({
                "pool": len(pool),
                "approved": len(approved),
                "rejected": len(rejected),
                "pending": len(pending),
            }, indent=2).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/sources.json":
            images = get_bbc_images(limit=MAX_IMAGE_POOL)
            counts = {"bbc": 0, "reuters": 0, "guardian": 0, "npr": 0, "other": 0}
            for img in images:
                lower = img.lower()
                if "bbci.co.uk" in lower:
                    counts["bbc"] += 1
                elif "reuters" in lower or "arcpublishing" in lower:
                    counts["reuters"] += 1
                elif "guim.co.uk" in lower or "theguardian" in lower:
                    counts["guardian"] += 1
                elif "npr" in lower or "brightspotcdn" in lower:
                    counts["npr"] += 1
                else:
                    counts["other"] += 1
            data = json.dumps({"total": len(images), "counts": counts, "sample": images[:40]}, indent=2).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/proxy":
            url = query.get("url", [""])[0]
            if not url:
                self.safe_send_bytes(400, b"Missing image URL")
                return
            if url_is_known_bad(url):
                REJECT_CACHE[url] = {"time": time.time()}
                self.safe_send_bytes(415, b"Known bad image", extra_headers={"Cache-Control": "no-store"})
                return
            rejected = REJECT_CACHE.get(url)
            if rejected and time.time() - rejected["time"] < REJECT_CACHE_SECONDS:
                self.safe_send_bytes(415, b"Rejected", extra_headers={"Cache-Control": "no-store"})
                return
            cached = PROXY_CACHE.get(url)
            if cached and time.time() - cached["time"] < PROXY_CACHE_SECONDS:
                self.safe_send_bytes(200, cached["data"], cached["content_type"], {"Cache-Control": "public, max-age=300"})
                return
            try:
                data, content_type = fetch_bytes(url, timeout=8)
                if not content_type.startswith("image/"):
                    REJECT_CACHE[url] = {"time": time.time()}
                    self.safe_send_bytes(415, b"Not an image", extra_headers={"Cache-Control": "no-store"})
                    return

                # If pre-vetted during pool build, skip all cv2 checks.
                if url in APPROVED_URLS:
                    PROXY_CACHE[url] = {"time": time.time(), "data": data, "content_type": content_type}
                    self.safe_send_bytes(200, data, content_type, {"Cache-Control": "public, max-age=300"})
                    return

                # Not yet vetted — size check only, no cv2 content filtering.
                try:
                    arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        ih, iw = img.shape[:2]
                        # Higher resolution floor for NPR — their images are often soft.
                        min_w = 1000 if "brightspotcdn" in url else MIN_IMAGE_WIDTH
                        min_h = 600 if "brightspotcdn" in url else MIN_IMAGE_HEIGHT
                        if iw < min_w or ih < min_h:
                            REJECT_CACHE[url] = {"time": time.time()}
                            self.safe_send_bytes(415, b"Rejected low resolution image", extra_headers={"Cache-Control": "no-store"})
                            return
                        if ih > iw * 1.4:
                            REJECT_CACHE[url] = {"time": time.time()}
                            self.safe_send_bytes(415, b"Rejected portrait", extra_headers={"Cache-Control": "no-store"})
                            return
                        cropped, did_crop = crop_top_if_needed(img, url)
                        if cropped is not None and cropped.size > 0:
                            ok, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
                            if ok:
                                data = encoded.tobytes()
                                content_type = "image/jpeg"
                except Exception:
                    pass
                APPROVED_URLS.add(url)
                PROXY_CACHE[url] = {"time": time.time(), "data": data, "content_type": content_type}
                self.safe_send_bytes(200, data, content_type, {"Cache-Control": "public, max-age=300"})
                return
            except Exception as e:
                REJECT_CACHE[url] = {"time": time.time()}
                print("[FETCH FAILED]", url, e)
                self.safe_send_bytes(502, b"Image fetch failed")
                return
        self.safe_send_bytes(404, b"Not found")


if __name__ == "__main__":
    REJECT_CACHE.clear()
    APPROVED_URLS.clear()
    print()
    print("misshurry")
    print("RSS + AP/Reuters/Guardian/NPR image pool: ON")
    print("Low-res rejection: ON")
    print(f"Serving at http://localhost:{PORT}")
    print()
    bg = threading.Thread(target=_background_pool_refresher, daemon=True)
    bg.start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
