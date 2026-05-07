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
    # Scene-first feeds. Removed BBC business / technology / health /
    # entertainment / science because they often produce headshots, podiums,
    # product shots, studio portraits, and generic cropped editorial images.
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "https://feeds.bbci.co.uk/news/in_pictures/rss.xml",
    "https://feeds.apnews.com/rss/apf-topnews",
    "https://www.theguardian.com/world/rss",
    "https://www.theguardian.com/us-news/rss",
    "https://www.npr.org/rss/rss.php?id=1001",
]

SOURCE_PAGES = [
    # AP/Reuters sections that tend to return event scenes instead of generic
    # tech/business/entertainment portrait cards.
    "https://apnews.com/",
    "https://apnews.com/world-news",
    "https://apnews.com/us-news",
    "https://apnews.com/politics",
    "https://apnews.com/sports",
    "https://apnews.com/climate-and-environment",
    "https://www.reuters.com/world/",
    "https://www.reuters.com/world/us/",
    "https://www.reuters.com/pictures/",
]


# Direct public section pages. These are scraped for image URLs because several
# non-BBC sources do not expose usable images through RSS.
DIRECT_IMAGE_PAGES = [
    # AP is favored because it tends to supply more event/scene photos than BBC cards.
    # Removed AP business / entertainment / technology / health / science hubs because
    # they often add generic portraits, product shots, conference panels, and crops.
    "https://apnews.com/",
    "https://apnews.com/world-news",
    "https://apnews.com/us-news",
    "https://apnews.com/politics",
    "https://apnews.com/sports",
    "https://apnews.com/climate-and-environment",
    "https://apnews.com/religion",
    "https://apnews.com/hub/ap-top-news",
    "https://apnews.com/hub/world-news",
    "https://apnews.com/hub/us-news",
    "https://apnews.com/hub/politics",
    "https://apnews.com/hub/sports",
    "https://apnews.com/hub/photography",
    "https://apnews.com/hub/photos",
    "https://apnews.com/hub/photo-gallery",
    "https://apnews.com/hub/ap-photos",
    "https://apnews.com/hub/europe",
    "https://apnews.com/hub/asia-pacific",
    "https://apnews.com/hub/africa",
    "https://apnews.com/hub/latin-america",
    "https://apnews.com/hub/middle-east",
    "https://apnews.com/hub/immigration",
    "https://apnews.com/hub/natural-disasters",
    "https://apnews.com/hub/ukraine",
    "https://apnews.com/hub/israel-hamas-war",

    # Reuters public pages can be inconsistent, but these are attempted briefly.
    "https://www.reuters.com/world/",
    "https://www.reuters.com/world/us/",
    "https://www.reuters.com/pictures/",

    # Extra public pages that usually expose straightforward image URLs.
    "https://www.theguardian.com/world",
    "https://www.theguardian.com/us-news",
    "https://www.npr.org/sections/news/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_IMAGE_POOL = 1400
SEQUENCE_LENGTH = 1100
IMAGE_CACHE = {"time": 0, "images": []}
CACHE_SECONDS = 60

PROXY_CACHE = {}
PROXY_CACHE_SECONDS = 300
PROXY_CACHE_MAX_ITEMS = 420

REJECT_CACHE = {}
REJECT_CACHE_SECONDS = 1800

MIN_IMAGE_WIDTH = 760
MIN_IMAGE_HEIGHT = 430

KNOWN_BAD_URL_FRAGMENTS = [
    "p0l7jnbt", "p0kxxp17", "p0n9y769", "3a08bc10", "b9785300",
    "4c3e0ce0", "9f35a8f0", "c471ab80",
    "c5a74450", "f53b6250", "p0ngd4cc",
    "00a03cc0", "929fd780", "06449360", "f4ee5fc0",
    "cfcd74b0", "7488a0b0", "72e83b70", "acb55400",
    "5a8f0590",
]

VERTICAL_ONLY_URL_FRAGMENTS = [
    "166137e0", "9a3df7e0", "b1e9ef60", "843ef730",
    "7b23ccb0", "c022fa90", "679152b0", "3600d2f0",
]

VOICE_CROP_URL_FRAGMENTS = ["f16b6b80"]


def url_is_known_bad(url):
    return any(fragment in url for fragment in KNOWN_BAD_URL_FRAGMENTS)


def url_is_vertical_only(url):
    return any(fragment in url for fragment in VERTICAL_ONLY_URL_FRAGMENTS)


def url_needs_voice_crop(url):
    return any(fragment in url for fragment in VOICE_CROP_URL_FRAGMENTS)


def url_is_disallowed_graphic_asset(url):
    if not url:
        return True
    lower = url.lower()
    if lower.endswith(".svg") or ".svg?" in lower:
        return True
    if "apnews" in lower and (lower.endswith(".png") or ".png?" in lower):
        return True
    if any(token in lower for token in ["typeshift", "pileup", "memoku"]):
        return True
    return False


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

    # AP/other news CDNs also host SVG/PNG graphics and UI art. These are
    # almost never the scene photos wanted for this project, so reject them
    # before they ever enter the slide pool.
    if lower.endswith(".svg") or ".svg?" in lower:
        return None
    if "apnews" in lower and (lower.endswith(".png") or ".png?" in lower):
        return None

    if any(bad in lower for bad in ["logo", "placeholder", "blank", "sprite", "icon", "type", "typeshift", "pileup", "memoku"]):
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
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def fetch_bytes(url, timeout=8):
    req = urllib.request.Request(url, headers=HEADERS)
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
        if any(domain in lower for domain in ["apnews.com", "reuters.com"]):
            if len(lower.rstrip('/').split('/')) < 4:
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
            if not any(token in lower for token in ["ichef.bbci", "assets.apnews", "dims.apnews", "cloudfront", "reuters", "guardian", "npr"]):
                continue
            seen.add(key)
            imgs.append(img)
            if len(imgs) >= max_images:
                return imgs
    return imgs


def extract_rss_item_image(item):
    media_ns = {"media": "http://search.yahoo.com/mrss/"}
    thumb = item.find("media:thumbnail", media_ns)
    if thumb is not None:
        return clean_extracted_image_url(thumb.attrib.get("url"))
    for media_content in item.findall("media:content", media_ns):
        url = media_content.attrib.get("url")
        mime = media_content.attrib.get("type", "")
        medium = media_content.attrib.get("medium", "")
        if url and (mime.startswith("image/") or medium == "image"):
            return clean_extracted_image_url(url)
    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url", "")
        mime = enclosure.attrib.get("type", "")
        if url and mime.startswith("image/"):
            return clean_extracted_image_url(url)
    description = item.find("description")
    if description is not None and description.text:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description.text, re.IGNORECASE)
        if m:
            return clean_extracted_image_url(m.group(1))
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
            "dims.apnews.com",
            "assets.apnews.com",
            "static.reuters.com",
            "cloudfront-us-east-2.images.arcpublishing.com",
            "media.guim.co.uk",
            "npr.brightspotcdn.com",
            ".jpg",
            ".jpeg",
            ".webp",
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

    # AP often hides the useful photo URLs in script/JSON blobs instead of img tags.
    # This second pass pulls those out directly.
    expanded = html_unescape_js_urls(html)
    url_patterns = [
        r'https://dims\.apnews\.com/[^"\'\s<>]+',
        r'https://assets\.apnews\.com/[^"\'\s<>]+',
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

def get_direct_page_images(limit=560):
    images = []
    seen = set()
    pages = DIRECT_IMAGE_PAGES[:]

    # AP gets its own larger crawl because AP's useful photos usually appear on
    # article pages / JSON blobs, not in a simple RSS image field.
    ap_pages = [p for p in pages if "apnews.com" in p]
    other_pages = [p for p in pages if "apnews.com" not in p]
    random.shuffle(ap_pages)
    random.shuffle(other_pages)
    pages = ap_pages + other_pages

    start_time = time.time()
    ap_page_budget_seconds = 14.0
    other_page_budget_seconds = 5.0
    ap_article_scrape_budget = 180
    other_article_scrape_budget = 30
    ap_article_scrapes = 0
    other_article_scrapes = 0

    def add_candidate(img):
        if not img:
            return False
        img = clean_extracted_image_url(img)
        if not img or url_is_known_bad(img) or url_is_disallowed_graphic_asset(img):
            return False
        key = normalize_image_url_for_dedupe(img)
        if not key or key in seen:
            return False
        seen.add(key)
        images.append(img)
        return True

    for page_url in pages:
        is_ap = "apnews.com" in page_url
        if len(images) >= limit:
            break
        elapsed = time.time() - start_time
        if is_ap:
            if elapsed > ap_page_budget_seconds and images:
                continue
        else:
            if elapsed > ap_page_budget_seconds + other_page_budget_seconds and images:
                break

        try:
            html = fetch_text(page_url, timeout=3.2 if is_ap else 2.4)

            # Inline/JSON images from section pages.
            candidates = extract_image_urls_from_html(html, page_url, limit=170 if is_ap else 55)
            # Keep AP early in the final list; shuffle within source so it still feels live.
            random.shuffle(candidates)
            for img in candidates:
                add_candidate(img)
                if len(images) >= limit:
                    break

            links = extract_article_links_from_html(
                html,
                page_url,
                max_links=120 if is_ap else 22,
            )
            random.shuffle(links)
            for link in links:
                if len(images) >= limit:
                    break
                if is_ap:
                    if ap_article_scrapes >= ap_article_scrape_budget:
                        break
                    ap_article_scrapes += 1
                else:
                    if other_article_scrapes >= other_article_scrape_budget:
                        break
                    other_article_scrapes += 1
                try:
                    article_html = fetch_text(link, timeout=2.8 if is_ap else 2.2)
                    article_imgs = extract_image_urls_from_html(article_html, link, limit=18 if is_ap else 5)
                    for img in article_imgs[:8 if is_ap else 3]:
                        add_candidate(img)
                        if len(images) >= limit:
                            break
                except Exception:
                    continue

        except Exception:
            continue

    # Do not shuffle the entire result: AP pages were intentionally crawled first,
    # so keeping this order gives AP more representation in the first rendered pool.
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
    if "apnews.com" in lower or "assets.apnews.com" in lower or "dims.apnews.com" in lower:
        return "ap"
    if "reuters" in lower:
        return "reuters"
    if "guim.co.uk" in lower or "theguardian" in lower:
        return "guardian"
    if "npr" in lower or "brightspotcdn" in lower:
        return "npr"
    if "bbci.co.uk" in lower or "bbc.co.uk" in lower:
        return "bbc"
    return "other"


def weighted_image_mix(images, limit=MAX_IMAGE_POOL):
    """Favor AP/Reuters/other non-BBC images so BBC cannot flood the final pool."""
    buckets = {"ap": [], "reuters": [], "guardian": [], "npr": [], "bbc": [], "other": []}
    for img in images:
        buckets.setdefault(source_category(img), []).append(img)

    for bucket in buckets.values():
        random.shuffle(bucket)

    # Keep this intentionally AP-heavy. If AP returns fewer images, the other
    # buckets fill the rest without causing errors.
    mixed = (
        buckets["ap"][:420]
        + buckets["reuters"][:220]
        + buckets["guardian"][:140]
        + buckets["npr"][:80]
        + buckets["other"][:80]
        + buckets["bbc"][:360]
    )

    remaining = []
    already = set(canonical_image_key(i) for i in mixed)
    for name in ["ap", "reuters", "guardian", "npr", "other", "bbc"]:
        for img in buckets[name]:
            key = canonical_image_key(img)
            if key not in already:
                already.add(key)
                remaining.append(img)
    random.shuffle(remaining)
    mixed.extend(remaining)
    return mixed[:limit]


def get_bbc_images(limit=MAX_IMAGE_POOL):
    now = time.time()
    if IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
        cached = IMAGE_CACHE["images"][:]
        random.shuffle(cached)
        return cached[:limit]

    images = []
    seen = set()
    start_time = time.time()
    time_budget_seconds = 18.0
    non_bbc_article_scrape_budget = 120
    non_bbc_article_scrapes = 0
    bbc_added = 0
    # Cap BBC so AP/Reuters/Guardian/NPR are not drowned out whenever BBC feeds are fast.
    max_bbc_images = int(limit * 0.34)

    def add_image(img):
        if not img:
            return False
        img = clean_extracted_image_url(img)
        if not img or url_is_known_bad(img) or url_is_disallowed_graphic_asset(img):
            return False
        key = canonical_image_key(img)
        if not key or key in seen:
            return False
        rejected = REJECT_CACHE.get(img)
        if rejected and now - rejected["time"] < REJECT_CACHE_SECONDS:
            return False
        seen.add(key)
        images.append(img)
        return True

    # Direct public pages first. This is the most reliable way to get AP scene images.
    for img in get_direct_page_images(limit=820):
        if len(images) >= limit:
            break
        add_image(img)

    feeds = RSS_FEEDS[:]
    # Keep non-BBC feeds before BBC feeds so they are not squeezed out by BBC volume.
    non_bbc_feeds = [f for f in feeds if not is_bbc_feed_url(f)]
    bbc_feeds = [f for f in feeds if is_bbc_feed_url(f)]
    random.shuffle(non_bbc_feeds)
    random.shuffle(bbc_feeds)
    feeds = non_bbc_feeds + bbc_feeds
    for feed_url in feeds:
        if len(images) >= limit:
            break
        if time.time() - start_time > time_budget_seconds and images:
            break
        try:
            rss = fetch_text(feed_url, timeout=2.5)
            root = ET.fromstring(rss)
            items = root.findall(".//item")
            random.shuffle(items)
            item_limit = 750 if is_bbc_feed_url(feed_url) else 90
            for item in items[:item_limit]:
                if len(images) >= limit:
                    break
                if is_bbc_feed_url(feed_url) and bbc_added >= max_bbc_images:
                    continue
                img = extract_rss_item_image(item)
                if add_image(img):
                    if is_bbc_feed_url(feed_url):
                        bbc_added += 1
                    continue
                if not is_bbc_feed_url(feed_url) and non_bbc_article_scrapes < non_bbc_article_scrape_budget:
                    link = item.find("link")
                    if link is not None and link.text:
                        non_bbc_article_scrapes += 1
                        add_image(extract_image_from_html_page(link.text.strip()))
        except Exception:
            continue

    pages = SOURCE_PAGES[:]
    random.shuffle(pages)
    page_article_scrapes = 0
    page_article_scrape_budget = 90
    for page_url in pages:
        if len(images) >= limit:
            break
        if time.time() - start_time > time_budget_seconds + 3.0 and images:
            break
        try:
            html = fetch_text(page_url, timeout=2.5)
            for img in extract_inline_images_from_html(html, page_url, max_images=60):
                add_image(img)
            links = extract_article_links_from_html(html, page_url, max_links=60)
            random.shuffle(links)
            for link in links:
                if len(images) >= limit or page_article_scrapes >= page_article_scrape_budget:
                    break
                page_article_scrapes += 1
                add_image(extract_image_from_html_page(link))
        except Exception:
            continue

    ordered = weighted_image_mix(images, limit=limit)

    IMAGE_CACHE["time"] = now
    IMAGE_CACHE["images"] = ordered[:]
    return ordered[:limit]


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

    # 1) Face/headshot rejection. Small crowd faces should pass; big centered faces do not.
    cascade = get_cv2_face_cascade()
    if cascade is not None:
        faces = cascade.detectMultiScale(
            gray,
            scaleFactor=1.08,
            minNeighbors=5,
            minSize=(max(34, int(w * 0.055)), max(34, int(h * 0.075))),
        )
        if len(faces) == 1:
            x, y, fw, fh = faces[0]
            face_area = (fw * fh) / float(w * h)
            cx = (x + fw / 2) / float(w)
            cy = (y + fh / 2) / float(h)
            centered = 0.25 < cx < 0.75 and 0.10 < cy < 0.62
            if centered and face_area > 0.012:
                return True
        elif len(faces) == 2:
            total_area = sum((fw * fh) for (x, y, fw, fh) in faces) / float(w * h)
            if total_area > 0.035:
                return True
        elif len(faces) >= 3:
            # Crowds/scenes often have many tiny faces; only reject when faces dominate.
            total_area = sum((fw * fh) for (x, y, fw, fh) in faces) / float(w * h)
            if total_area > 0.075:
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
        (border_edge < 0.030 and border_std < 42 and border_unique < 95)
        or (border_edge < 0.022 and border_sat_std < 30 and border_unique < 80)
        or (border_edge < 0.020 and border_sat_mean < 55 and border_val_mean > 92)
    )
    isolated_subject = center_edge > max(0.052, border_edge * 2.15)

    if plain_background and isolated_subject:
        return True

    # 3) Reject obvious single-person waist-up crops even if the face detector misses.
    # Skin-ish blob centered + low-detail border is usually a generic portrait.
    y0, y1 = int(h * 0.08), int(h * 0.78)
    x0, x1 = int(w * 0.20), int(w * 0.80)
    crop_hsv = hsv[y0:y1, x0:x1, :]
    if crop_hsv.size:
        hue = crop_hsv[:, :, 0]
        sat_c = crop_hsv[:, :, 1]
        val_c = crop_hsv[:, :, 2]
        skinish = ((hue < 24) | (hue > 165)) & (sat_c > 35) & (sat_c < 185) & (val_c > 55)
        skinish_frac = float(np.mean(skinish))
        if skinish_frac > 0.085 and border_edge < 0.038 and border_unique < 125:
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
    divider_x = center_min + int(np.argmax(center_energy))
    peak_energy = float(col_energy[divider_x])
    baseline = float(np.median(col_energy)) + 1e-6
    if peak_energy < baseline * 2.2:
        return False
    col_slice = edge_strength[:, max(0, divider_x - 1):min(w, divider_x + 2)]
    row_strength = col_slice.mean(axis=1)
    row_baseline = float(np.median(row_strength)) + 1e-6
    strong_frac = float(np.mean(row_strength > row_baseline * 1.55))
    return strong_frac > 0.38


def render_html():
    images = get_bbc_images(limit=MAX_IMAGE_POOL)
    sequence = []
    for img in images:
        proxied = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({"src": proxied, "raw": img, "verticalOnly": url_is_vertical_only(img)})
    sequence_json = json.dumps(sequence)
    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>misshurry</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
html, body {{ margin:0; padding:0; width:100%; height:100%; overflow:hidden; background:#000; cursor:crosshair; }}
canvas {{ display:block; width:100vw; height:100vh; touch-action:none; }}
#debug-url {{ position:fixed; bottom:8px; left:50%; transform:translateX(-50%); color:rgba(255,255,255,.52); font:11px monospace; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:90vw; z-index:50; cursor:copy; user-select:none; pointer-events:auto; background:rgba(0,0,0,.28); padding:3px 6px; border-radius:4px; }}
</style>
</head>
<body>
<div id="debug-url"></div>
<canvas id="view"></canvas>
<script>
let slides = {sequence_json};
slides = slides.filter(s => !/\.svg(\?|$)/i.test(s.raw || s.src) && !/(typeshift|pileup|memoku)/i.test(s.raw || s.src));
const SEQUENCE_LENGTH_JS = {SEQUENCE_LENGTH};
const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d", {{ willReadFrequently: true }});
let currentPrepared = null, currentImage = null, currentSrc = null;
let mouseX = 0, mouseY = 0, DPR = 1, VIEW_W = window.innerWidth, VIEW_H = window.innerHeight;
let shuffledPool = [], poolIndex = 0, isLoadingSlide = false;
let recentlyShown = [];
let badSrcs = new Set();
const RECENT_LIMIT = 40;

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
function refillPool() {{
  let candidates = slides
    .filter(slideAllowedForCurrentOrientation)
    .map(s => s.src)
    .filter(src => !badSrcs.has(src));

  if (currentSrc && candidates.length > 1) candidates = candidates.filter(src => src !== currentSrc);

  let fresh = candidates.filter(src => !recentlyShown.includes(src));

  // If the surviving pool is smaller than recent memory, relax recent memory
  // instead of forcing the page to repeat one tiny subset forever.
  if (fresh.length < Math.min(12, candidates.length)) fresh = candidates;

  shuffledPool = shuffleArray(fresh);
  poolIndex = 0;

  console.log("rotation pool", {{
    totalSlides: slides.length,
    usable: candidates.length,
    fresh: fresh.length,
    bad: badSrcs.size,
    recent: recentlyShown.length
  }});
}}
function getNextRandomSrc() {{ if (!shuffledPool.length || poolIndex >= shuffledPool.length) refillPool(); if (!shuffledPool.length) return null; return shuffledPool[poolIndex++]; }}
function makeImage(sourceImage) {{
  const off = document.createElement("canvas"); off.width = canvas.width; off.height = canvas.height;
  const offCtx = off.getContext("2d", {{ willReadFrequently: true }}); syncContextQuality(offCtx);
  offCtx.fillStyle = "#000"; offCtx.fillRect(0,0,off.width,off.height);
  const fit = fitCover(sourceImage.width, sourceImage.height, off.width, off.height);
  offCtx.drawImage(sourceImage, 0,0, sourceImage.width, sourceImage.height, fit.x, fit.y, fit.w, fit.h);
  const imageData = offCtx.getImageData(0,0,off.width,off.height); const data = imageData.data;
  const levels = 28; const step = 255/(levels-1);
  for(let i=0;i<data.length;i+=4) {{ let gray = 0.299*data[i]+0.587*data[i+1]+0.114*data[i+2]; gray = Math.round(gray/step)*step; data[i]=gray; data[i+1]=gray; data[i+2]=gray; data[i+3]=255; }}
  offCtx.putImageData(imageData,0,0); return off;
}}
function drawFallbackMessage() {{ ctx.clearRect(0,0,canvas.width,canvas.height); ctx.fillStyle="#000"; ctx.fillRect(0,0,canvas.width,canvas.height); }}
function drawFlashlight() {{
  if (!currentPrepared) {{ drawFallbackMessage(); return; }}
  const isTouchDevice = window.matchMedia("(pointer: coarse)").matches;
  const radius = Math.sqrt(canvas.width*canvas.width + canvas.height*canvas.height) * (isTouchDevice ? 0.13 : 0.075);
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
  const rawUrl = decodeURIComponent(src.replace("/proxy?url=", ""));
  const usableEstimate = Math.max(0, slides.length - badSrcs.size);
  const el = document.getElementById("debug-url");
  el.textContent = rawUrl + "  [recent " + recentlyShown.length + " / usable ~" + usableEstimate + " / bad " + badSrcs.size + "]";
  el.title = "Click to copy image URL"; el.dataset.url = rawUrl;
}}
function loadRandomSlide(attempts=0) {{
  // Do not let one slow request freeze the whole rotation.
  if (isLoadingSlide && attempts === 0) return;
  isLoadingSlide = true;
  resizeCanvas();

  if (!slides.length || attempts > 90) {{
    if (badSrcs.size > 0) {{
      console.log("resetting badSrcs after too many failed attempts", badSrcs.size);
      badSrcs.clear();
      refillPool();
    }}
    isLoadingSlide = false;
    return;
  }}

  const src = getNextRandomSrc();
  if (!src) {{
    isLoadingSlide = false;
    setTimeout(() => {{ refillPool(); loadRandomSlide(attempts + 1); }}, 80);
    return;
  }}

  const loader = new Image();
  loader.decoding = "async";

  let finished = false;
  const finish = (worked) => {{
    if (finished) return;
    finished = true;
    clearTimeout(loadTimer);
    isLoadingSlide = false;
    if (!worked) {{
      badSrcs.add(src);
      shuffledPool = shuffledPool.filter(s => s !== src);
      setTimeout(() => loadRandomSlide(attempts + 1), 35);
    }}
  }};

  const loadTimer = setTimeout(() => {{
    console.log("image load timeout", src);
    finish(false);
  }}, 2400);

  loader.onload = () => {{
    if (!isVerticalPhone() && loader.naturalHeight > loader.naturalWidth * 1.08) {{
      finish(false);
      return;
    }}
    prepareAndDraw(loader, src);
    finish(true);
  }};

  loader.onerror = () => {{
    finish(false);
  }};

  loader.src = src;
}}
function updateFlashlightPositionFromPointer(e) {{ const rect=canvas.getBoundingClientRect(); const isTouchDevice=window.matchMedia("(pointer: coarse)").matches; const offsetY=isTouchDevice ? window.innerHeight*0.12 : 0; mouseX=(e.clientX-rect.left)*DPR; mouseY=((e.clientY-rect.top)-offsetY)*DPR; drawFlashlight(); }}
canvas.addEventListener("pointermove", updateFlashlightPositionFromPointer);
const debugUrlEl = document.getElementById("debug-url");
debugUrlEl.addEventListener("click", async (e) => {{ e.stopPropagation(); const url=debugUrlEl.dataset.url || debugUrlEl.textContent; if(!url) return; try {{ await navigator.clipboard.writeText(url); const oldText=debugUrlEl.textContent; debugUrlEl.textContent="copied"; setTimeout(() => {{ debugUrlEl.textContent=oldText; }}, 650); }} catch(err) {{ window.prompt("Copy image URL:", url); }} }});
window.addEventListener("resize", () => {{ resizeCanvas(); refillPool(); if(currentImage) {{ currentPrepared = makeImage(currentImage); drawFlashlight(); }} else {{ loadRandomSlide(); }} }});
resizeCanvas(); mouseX=canvas.width/2; mouseY=canvas.height/2; refillPool(); loadRandomSlide(); setInterval(loadRandomSlide, 2800);
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

        if path == "/sources.json":
            images = get_bbc_images(limit=MAX_IMAGE_POOL)
            counts = {"bbc": 0, "ap": 0, "reuters": 0, "guardian": 0, "npr": 0, "other": 0}
            for img in images:
                lower = img.lower()
                if "bbci.co.uk" in lower:
                    counts["bbc"] += 1
                elif "apnews.com" in lower or "assets.apnews.com" in lower:
                    counts["ap"] += 1
                elif "reuters" in lower:
                    counts["reuters"] += 1
                elif "guim.co.uk" in lower or "theguardian" in lower:
                    counts["guardian"] += 1
                elif "npr" in lower or "brightspotcdn" in lower:
                    counts["npr"] += 1
                else:
                    counts["other"] += 1

            ap_sample = [img for img in images if "apnews.com" in img.lower()][:40]
            data = json.dumps({"total": len(images), "counts": counts, "ap_sample": ap_sample, "sample": images[:40]}, indent=2).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/proxy":
            url = query.get("url", [""])[0]
            if not url:
                self.safe_send_bytes(400, b"Missing image URL")
                return
            if url_is_known_bad(url) or url_is_disallowed_graphic_asset(url):
                REJECT_CACHE[url] = {"time": time.time()}
                print("[REJECT known bad/graphic asset]", url)
                self.safe_send_bytes(415, b"Known bad or graphic asset", extra_headers={"Cache-Control": "no-store"})
                return
            cleanup_proxy_cache()
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
                if "svg" in content_type.lower() or url_is_disallowed_graphic_asset(url):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT svg/png graphic asset]", url)
                    self.safe_send_bytes(415, b"Rejected graphic asset", extra_headers={"Cache-Control": "no-store"})
                    return
                test_data = data
                try:
                    arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        ih, iw = img.shape[:2]
                        if iw < MIN_IMAGE_WIDTH or ih < MIN_IMAGE_HEIGHT:
                            REJECT_CACHE[url] = {"time": time.time()}
                            print("[REJECT low resolution]", url, iw, ih)
                            self.safe_send_bytes(415, b"Rejected low resolution image", extra_headers={"Cache-Control": "no-store"})
                            return
                        if image_is_probably_full_graphic_page(data):
                            REJECT_CACHE[url] = {"time": time.time()}
                            print("[REJECT graphic pre-crop]", url)
                            self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                            return
                        cropped, did_crop = crop_top_if_needed(img, url)
                        if cropped is not None and cropped.size > 0:
                            ok, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 98])
                            if ok:
                                data = encoded.tobytes()
                                test_data = data
                                content_type = "image/jpeg"
                                if did_crop:
                                    print("[CROP top]", url)
                except Exception:
                    test_data = data
                if image_is_probably_full_graphic_page(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT graphic]", url)
                    self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                    return
                if image_is_portrait_or_generic_isolated_subject(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT portrait/generic isolated]", url)
                    self.safe_send_bytes(415, b"Rejected portrait or generic isolated subject", extra_headers={"Cache-Control": "no-store"})
                    return
                if image_has_center_divider(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT divider]", url)
                    self.safe_send_bytes(415, b"Rejected center divider", extra_headers={"Cache-Control": "no-store"})
                    return
                print("[SERVE]", url)
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
    print()
    print("misshurry")
    print("RSS + AP/Reuters/Guardian/NPR image pool: ON")
    print("Low-res rejection: ON")
    print(f"Serving at http://localhost:{PORT}")
    print()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
