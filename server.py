# server.py

```python
# Updated scene-weighted news image scraper
# Prioritizes AP/Reuters scene photography over generic BBC portraits.

import json
import os
import random
import re
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

PORT = int(os.environ.get("PORT", 8000))

RSS_FEEDS = [
    # BBC kept intentionally limited
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://feeds.bbci.co.uk/news/in_pictures/rss.xml",

    # AP
    "https://feeds.apnews.com/rss/apf-topnews",

    # Guardian / NPR
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/us-news/rss",
    "https://www.npr.org/rss/rss.php?id=1001",
]

DIRECT_IMAGE_PAGES = [
    # AP
    "https://apnews.com/",
    "https://apnews.com/world-news",
    "https://apnews.com/us-news",
    "https://apnews.com/politics",
    "https://apnews.com/sports",
    "https://apnews.com/science",

    # Reuters
    "https://www.reuters.com/world/",
    "https://www.reuters.com/world/us/",
    "https://www.reuters.com/pictures/",

    # Guardian
    "https://www.theguardian.com/world",
    "https://www.theguardian.com/us-news",

    # NPR
    "https://www.npr.org/sections/news/",
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

MAX_IMAGE_POOL = 1100
CACHE_SECONDS = 60

IMAGE_CACHE = {"time": 0, "images": []}
PROXY_CACHE = {}
REJECT_CACHE = {}

PROXY_CACHE_SECONDS = 300
REJECT_CACHE_SECONDS = 1800

MIN_IMAGE_WIDTH = 720
MIN_IMAGE_HEIGHT = 420

KNOWN_BAD_URL_FRAGMENTS = [
    "p0l7jnbt", "p0kxxp17", "p0n9y769",
    "00a03cc0", "929fd780", "06449360",
    "5a8f0590",
]

VERTICAL_ONLY_URL_FRAGMENTS = [
    "166137e0", "9a3df7e0", "7b23ccb0",
    "c022fa90", "679152b0", "3600d2f0",
]

VOICE_CROP_URL_FRAGMENTS = ["f16b6b80"]


def url_is_known_bad(url):
    return any(fragment in url for fragment in KNOWN_BAD_URL_FRAGMENTS)


def url_is_vertical_only(url):
    return any(fragment in url for fragment in VERTICAL_ONLY_URL_FRAGMENTS)


def url_needs_voice_crop(url):
    return any(fragment in url for fragment in VOICE_CROP_URL_FRAGMENTS)


def fetch_text(url, timeout=4):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def fetch_bytes(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type", "application/octet-stream")


def upgrade_bbc_image_url(url):
    if not url:
        return url

    url = url.replace("/240/", "/1024/")
    url = url.replace("/320/", "/1024/")
    url = url.replace("/480/", "/1024/")
    url = re.sub(r"/ic/\d+x\d+/", "/ic/1024x576/", url)
    url = re.sub(r"/standard/\d+/", "/standard/1024/", url)
    return url


def canonical_image_key(url):
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    inner = qs.get("url", [None])[0]
    if inner:
        return canonical_image_key(inner)

    return parsed.netloc + parsed.path


def clean_extracted_image_url(url):
    if not url:
        return None

    url = url.strip().replace("&amp;", "&")

    if url.startswith("//"):
        url = "https:" + url

    if not url.startswith("http"):
        return None

    lower = url.lower()

    if any(bad in lower for bad in [
        "logo",
        "icon",
        "placeholder",
        "avatar",
        "sprite",
    ]):
        return None

    return upgrade_bbc_image_url(url)


def extract_image_urls_from_html(html, base_url, limit=80):
    found = []

    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<img[^>]+src=["\']([^"\']+)["\']',
    ]

    for pattern in patterns:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            img = clean_extracted_image_url(m.group(1))

            if not img:
                continue

            lower = img.lower()

            if not any(x in lower for x in [
                "bbci.co.uk",
                "apnews",
                "reuters",
                "guim.co.uk",
                "npr",
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
            ]):
                continue

            found.append(img)

            if len(found) >= limit:
                return found

    return found


def extract_rss_item_image(item):
    media_ns = {"media": "http://search.yahoo.com/mrss/"}

    thumb = item.find("media:thumbnail", media_ns)
    if thumb is not None:
        return clean_extracted_image_url(thumb.attrib.get("url"))

    for media_content in item.findall("media:content", media_ns):
        url = media_content.attrib.get("url")
        if url:
            return clean_extracted_image_url(url)

    description = item.find("description")

    if description is not None and description.text:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description.text)
        if m:
            return clean_extracted_image_url(m.group(1))

    return None


def image_is_soft_or_low_detail(data):
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None:
        return False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    lap_var = cv2.Laplacian(gray, cv2.CV_64F).var()

    edges = cv2.Canny(gray, 70, 170)
    edge_density = float(np.mean(edges > 0))

    return lap_var < 42 and edge_density < 0.045


def image_is_center_subject_portrait(data):
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None:
        return False

    h, w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 70, 170)

    center = edges[:, int(w * 0.28):int(w * 0.72)]

    left = edges[:, :int(w * 0.18)]
    right = edges[:, int(w * 0.82):]

    outer = np.concatenate([left.flatten(), right.flatten()])

    center_density = float(np.mean(center > 0))
    outer_density = float(np.mean(outer > 0))

    return center_density > outer_density * 2.8 and outer_density < 0.035


def get_bbc_images(limit=MAX_IMAGE_POOL):
    now = time.time()

    if IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
        cached = IMAGE_CACHE["images"][:]
        random.shuffle(cached)
        return cached[:limit]

    images = []
    seen = set()

    time_budget_seconds = 18.0
    start_time = time.time()

    non_bbc_article_scrape_budget = 120
    page_article_scrape_budget = 90

    bbc_feeds = [f for f in RSS_FEEDS if "bbc" in f]
    non_bbc_feeds = [f for f in RSS_FEEDS if "bbc" not in f]

    random.shuffle(non_bbc_feeds)
    random.shuffle(bbc_feeds)

    feeds = non_bbc_feeds + bbc_feeds

    def add_image(img):
        if not img:
            return False

        if url_is_known_bad(img):
            return False

        key = canonical_image_key(img)

        if not key or key in seen:
            return False

        seen.add(key)
        images.append(img)
        return True

    for feed_url in feeds:
        if time.time() - start_time > time_budget_seconds:
            break

        try:
            rss = fetch_text(feed_url, timeout=3)
            root = ET.fromstring(rss)
            items = root.findall(".//item")

            random.shuffle(items)

            for item in items[:160]:
                img = extract_rss_item_image(item)
                add_image(img)

        except Exception:
            continue

    pages = DIRECT_IMAGE_PAGES[:]
    random.shuffle(pages)

    for page_url in pages:
        try:
            html = fetch_text(page_url, timeout=3)

            for img in extract_image_urls_from_html(html, page_url, limit=60):
                add_image(img)

        except Exception:
            continue

    ap = [i for i in images if "apnews" in i or "assets.apnews" in i]
    reuters = [i for i in images if "reuters" in i]
    bbc = [i for i in images if "bbci.co.uk" in i]
    other = [i for i in images if i not in ap and i not in reuters and i not in bbc]

    random.shuffle(ap)
    random.shuffle(reuters)
    random.shuffle(bbc)
    random.shuffle(other)

    images = (
        ap[:260]
        + reuters[:180]
        + other[:120]
        + bbc[:320]
    )

    random.shuffle(images)

    IMAGE_CACHE["time"] = now
    IMAGE_CACHE["images"] = images[:]

    return images[:limit]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def safe_write(self, data):
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def safe_send_bytes(self, status_code, data, content_type="text/plain"):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/sources.json":
            images = get_bbc_images()
            data = json.dumps(images[:100], indent=2).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json")
            return

        self.safe_send_bytes(404, b"Not found")


if __name__ == "__main__":
    print()
    print("Scene-weighted news image scraper")
    print(f"Serving at http://localhost:{PORT}")
    print()

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
```

---

# test.py

```python
from server import (
    url_is_known_bad,
    url_is_vertical_only,
)

TESTS = [
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/5a8f0590.jpg",
        True,
        False,
        "generic editorial image should be rejected",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/166137e0.jpg",
        False,
        True,
        "vertical-only image should pass vertical rule",
    ),
]


def main():
    failures = 0

    for url, expected_bad, expected_vertical, label in TESTS:
        got_bad = url_is_known_bad(url)
        got_vertical = url_is_vertical_only(url)

        ok = (
            got_bad == expected_bad
            and got_vertical == expected_vertical
        )

        if ok:
            print("PASS ✓", label)
        else:
            print("FAIL ✗", label)
            failures += 1

    print()
    print(f"{len(TESTS) - failures} passed, {failures} failed")


if __name__ == "__main__":
    main()
```

