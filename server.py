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

    # France 24 — international feeds only, no French national news.
    "https://www.france24.com/en/middle-east/rss",
    "https://www.france24.com/en/americas/rss",
    "https://www.france24.com/en/europe/rss",
    "https://www.france24.com/en/africa/rss",
    "https://www.france24.com/en/asia-pacific/rss",

    # CGTN — blocked or returns no images from Render's IPs.

    # South China Morning Post — Hong Kong English-language, world + Asia coverage.
    "https://www.scmp.com/rss/5/feed",
    "https://www.scmp.com/rss/4/feed",

    # Deutsche Welle — blocked by Cloudflare from Render's IPs.
    # CBC Canada — working, 19 images.
    "https://www.cbc.ca/webfeed/rss/rss-world",

    # Mexico News Daily — blocked by Cloudflare from Render's IPs.
]

SOURCE_PAGES = [
    "https://www.theguardian.com/",
    "https://www.theguardian.com/world",
    "https://www.theguardian.com/us-news",
    "https://www.theguardian.com/politics",
    "https://www.theguardian.com/global-development",
    "https://www.theguardian.com/law",
    "https://www.theguardian.com/society",
    "https://www.theguardian.com/environment",
    "https://www.aljazeera.com/news/",
    "https://www.aljazeera.com/where/middle-east/",
    "https://www.aljazeera.com/where/africa/",
    "https://www.aljazeera.com/where/asia/",
    "https://www.france24.com/en/",
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
REJECT_CACHE_SECONDS = 300  # 5 minutes — retry failed images sooner

# URLs that passed cv2 checks during pool build — skip checks at serve time.
APPROVED_URLS = set()
APPROVED_VERTICAL_URLS = set()  # subset of APPROVED_URLS that are vertical-friendly (mobile only)

GUARDIAN_API_ENABLED = True
GUARDIAN_API_KEY = "92e99e1d-f706-45be-b06a-35af28e94141"
GUARDIAN_API_SECTIONS = [
    "world", "us-news", "politics",
    "global-development", "society",
    "business", "law", "cities",
]
# Sections that trend toward archival/stock imagery — fetch fewer pages from these.
GUARDIAN_API_SLOW_SECTIONS = {"society", "business", "cities"}


GUARDIAN_PRIORITY_SECTIONS = {"world", "us-news", "politics", "global-development", "law"}
GUARDIAN_IMAGE_SECTION = {}  # url -> section
GUARDIAN_IMAGE_DATE = {}  # url -> webPublicationDate (ISO string), for true newest-first ordering


def fetch_guardian_api_images(limit=200):
    """Fetch images from Guardian open content API — structured, no scraping needed."""
    images = []
    seen = set()
    for section in GUARDIAN_API_SECTIONS:
        if len(images) >= limit:
            break
        try:
            page_size = 25 if section in GUARDIAN_API_SLOW_SECTIONS else 50
            url = (
                f"https://content.guardianapis.com/search"
                f"?section={section}&show-fields=main&page-size={page_size}"
                f"&type=article&order-by=newest&api-key={GUARDIAN_API_KEY}"
            )
            data = fetch_text(url, timeout=3)
            blob = json.loads(data)
            results = blob.get("response", {}).get("results", [])
            for item in results:
                fields = item.get("fields", {})
                img_url = fields.get("main", "")
                if not img_url:
                    continue
                pub_date = item.get("webPublicationDate", "")
                # Skip Guardian API articles older than 30 days
                if pub_date:
                    try:
                        import datetime as _dt
                        pub_ts = _dt.datetime.strptime(pub_date[:19], "%Y-%m-%dT%H:%M:%S").replace(
                            tzinfo=_dt.timezone.utc).timestamp()
                        if (time.time() - pub_ts) / 86400 > 30:
                            continue
                    except Exception:
                        pass
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
                    GUARDIAN_IMAGE_SECTION[cleaned] = section
                    if pub_date:
                        GUARDIAN_IMAGE_DATE[cleaned] = pub_date
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
GUARDIAN_API_CACHE_SECONDS = 3600  # 1 hour — free dev tier ~500-5000 req/day, 9 sections × 24 = 216 calls/day max
GUARDIAN_API_CACHE_FILE = "/tmp/guardian_api_cache.json"


def _load_guardian_cache_from_disk():
    """Load Guardian API cache from disk on startup to survive restarts."""
    try:
        with open(GUARDIAN_API_CACHE_FILE, "r") as f:
            data = json.load(f)
        age = time.time() - data.get("time", 0)
        if age < GUARDIAN_API_CACHE_SECONDS and data.get("images"):
            GUARDIAN_API_CACHE["images"] = data["images"]
            GUARDIAN_API_CACHE["time"] = data["time"]
            print(f"[Guardian API] Loaded {len(data['images'])} images from disk cache (age {int(age/3600)}h)", flush=True)
    except Exception:
        pass


def _save_guardian_cache_to_disk(images, timestamp):
    """Persist Guardian API cache to disk."""
    try:
        with open(GUARDIAN_API_CACHE_FILE, "w") as f:
            json.dump({"images": images, "time": timestamp}, f)
        print(f"[Guardian API] Saved {len(images)} images to disk cache", flush=True)
    except Exception as e:
        print(f"[Guardian API] Failed to save disk cache: {e}", flush=True)


def get_guardian_api_images():
    if not GUARDIAN_API_ENABLED:
        return []
    now = time.time()
    if GUARDIAN_API_CACHE["images"] and now - GUARDIAN_API_CACHE["time"] < GUARDIAN_API_CACHE_SECONDS:
        print(f"[Guardian API] Using cache: {len(GUARDIAN_API_CACHE['images'])} images", flush=True)
        return GUARDIAN_API_CACHE["images"][:]
    print("[Guardian API] Fetching fresh images …", flush=True)
    images = fetch_guardian_api_images(limit=600)
    print(f"[Guardian API] Got {len(images)} images", flush=True)
    if images:
        GUARDIAN_API_CACHE["images"] = images
        GUARDIAN_API_CACHE["time"] = now
        _save_guardian_cache_to_disk(images, now)
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
    "86cf2180-68ca-11f1-b1db-af71d47507d6",
    "a2e8a279-839a-40d3-83f8-dc65df0dbc72",
    "48c2f1158b35ff75cb2edcbb613c747af1215f74",
    "321b5880-4a2e-11f1-91d3-69962f9a0625",
    "d91d9250-6b22-11f1-b1db-af71d47507d6",
    "03c3e400-6b19-11f1-be36-65d2d6d55e70",
    "356ab330-6baf-11f1-b1db-af71d47507d6",
    "e4af7040-6bed-11f1-b1db-af71d47507d6",
    "b18388cd352963f68433649ecac15555847868a5",
    "348c2a8b427d556c9e9e53ab40fe2b292e305b6d",
    "b3962d03da5e05ce790c02cf6de8f24dc3b59578",
    "40cca0401fe2c7d427bb3fbb79f43c0007165eb3",
    "c5f90dd0-6bcc-11f1-8e1d-bbbb1017d210",
    "ad8daf8b33ef8da2bcac89b8d0a5d6ab64e2f3ed",
    "Post-Label-Image-Option-1-1780213226",
    "Ahmed-Wishah-1781977869",
    "e35ca6a4be4b3a80e6eb5c4f9e9711956b208758",
    "image-1781976244",
    "image-1782060560",
    "48cc8790-6bf1-11f1-b1db-af71d47507d6",
    "image-1782053254",
    "349c0fd155396d27a63304946d6c679eab8161b2",
    "d5e431a0-6e35-11f1-8546-8f19e4fe30f4",
    "image-1782128484",
    "e785ef90-69ce-11f1-84fd-21e83c1eab66",
    "b849f2c145d90891839dadff9b76ee9d555a0ba4",
    "IMG-20250912-WA0000-1757829229",
    "03b31d2b27c92f1f3b9d859af311bdc32e1a5b1d",
    "a986591a5616e58ef4e969da4c262a7cd5ea966d",
    "b88185dfdfb96f9906fd85f7be019566c90544c9",
    "3d1c142050b20ca18ed327ed36c5d09a84a61f8d",
    "4c7168712c6d206d96b7ff78ac94bcb2958fe483",
    "cf9a22baba3717df477614306e6da87bd7727226",
    "31a31cfa-6bc9-11f1-aa70-005056a97e36",
    "d2d1c8e2-6e1c-11f1-914d-005056a97e36",
    "00e8a2ecccc606efdbeab5493a4a65b752cf9beb",
    "9d7f0c052d5b5056ea7f75885737bc3437e696f9",
    "image-1782140464",
    "2866b514-5a78-11f1-a386-005056a90284",
    "VORONEZH-RUSSIA-1000x562-1782131876",
    "7f55a627cb463145d860af987aa8e28af2da3a43",
    "c9852750-6e28-11f1-8e1d-bbbb1017d210",
    "Pattni",
    "cd0153f0-6e63-11f1-8546-8f19e4fe30f4",
    "76ee748f407ab07b21706772d56f",
    "image-1781726961",
    "image-1781611733",
    "86ca4870-6e0d-11f1-8546-8f19e4fe30f4",
    "d79b2b10-6e22-11f1-8546-8f19e4fe30f4",
    "0c1e7cd6-69a8-11f1-a995-005056bfb2b6",
    "3c55cc7e-245e-4a29-a845-1d462fa0e9f4",
    "76acff10-6d7b-11f1-a2ba-775ae811ce10",
    "ccff1ef8-c0b7-4dd3-bf0e-9b98ee86f672",
    "30742380-6e51-11f1-8c89-cfc50446b805",
    "IMG_9701-1782142787",
    "cbb03ddb-cbc4-41ac-8002-ab4975cef551",
    "5a012522-64a6-11f1-9883-005056a90284",
    "b01d2756-b2aa-460a-836d-5878468f97b9",
    "f1fca7c0-6e42-11f1-b1db-af71d47507d6",
    "5352d20b-415f-4b02-a401-34d6a39c0b6b",
    "0f6dbd0a-1f62-4513-9a1f-c3a8e1367883",
    "e1968e79-a02c-463a-9942-e5b287de3b70",
    "d88f5710-6dca-11f1-8546-8f19e4fe30f4",
    "5619ce28-7c62-4d46-8675-df81ac83b401",
    "2cba3e70-6ec2-11f1-8e1d-bbbb1017d210",
    "efd8f892-718d-11f1-bf82-005056a97e36",
    "638d53fb17f4382512f8a335204cb99e8749b610",
    "5b881c5d-d89d-4e55-a3f7-74c9ec81c11a",
    "image00002-1782318053",
    "3543438e-719c-11f1-b3ec-005056bf30b7",
    "96818070-8d68-11f0-9cf6-cbf3e73ce2b9",
    "a4cf463a-70e3-11f1-b99c-005056bf30b7",
    "image-1778852411",
    "image-1782674411",
    "4640960b-7b2c-4f60-addc-0718a06a0548",
    "f35a66c0-7879-11f1-9510-1546718f668b",
    "d5fba34000f1938b434ca92fb39c16149e786788",
    "4ed911a0-4e59-4835-a882-2561b95e121b",
    "3525a160-7888-11f1-a627-714adb4eed6e",
    "6a465240-76f3-11f1-b976-0b9c15b0ccfc",
    "011d81c0-7933-11f1-a627-714adb4eed6e",
    "ed0503f9-796e-4422-9d64-7457b94fc27e",
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
    # Mercopress images are cached at small sizes (e.g. /600x315/) —
    # try upgrading to a larger cached size.
    if "mercopress.com" in url:
        url = re.sub(r'/\d+x\d+/', '/1280x720/', url)
    url = url.replace("/240/", "/2048/")
    url = url.replace("/320/", "/2048/")
    url = url.replace("/480/", "/2048/")
    url = url.replace("/624/", "/2048/")
    url = url.replace("/660/", "/2048/")
    url = url.replace("/1024/", "/2048/")
    url = re.sub(r"/ic/\d+x\d+/", "/ic/2048x1152/", url)
    url = re.sub(r"/standard/\d+/", "/standard/2048/", url)
    # AP's dims.apnews.com proxy has a /resize/WxH!/ segment specifying the
    # final output size, separate from the /crop/ region. Scale it up while
    # preserving its aspect ratio (forcing a fixed ratio caused 400 errors
    # when it didn't match the crop). Cap the upscale so we don't trigger
    # excessive CDN load (which was worsening 429 rate limiting).
    def _upgrade_ap_resize(match):
        rw, rh = int(match.group(1)), int(match.group(2))
        if rw <= 0 or rh <= 0:
            return match.group(0)
        target_w = min(max(rw, 1440), 1800)
        scale = target_w / rw
        target_h = max(1, round(rh * scale))
        return f"/resize/{target_w}x{target_h}!/"
    url = re.sub(r'/resize/(\d+)x(\d+)!/', _upgrade_ap_resize, url)
    # Al Jazeera WordPress images often cap quality=80 in the resize query
    # param — bump it up for a sharper result.
    if "aljazeera.com" in url:
        url = re.sub(r'quality=\d+', 'quality=95', url)
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
    # Al Jazeera's WordPress CDN serves some images from thumbnail-derived
    # source assets (filename contains "thumb") which stay soft even when
    # resized larger — reject these rather than show a blurry upscale.
    if "aljazeera.com" in lower and "thumb" in lower:
        return None
    # Al Jazeera PNG files are always graphics/overlays, not news photos.
    if "aljazeera.com" in lower and (lower.endswith(".png") or ".png?" in lower):
        return None
    # Al Jazeera WordPress URLs include upload year in path (/2023/03/) —
    # Reject Al Jazeera images older than 30 days using the WordPress URL date.
    if "aljazeera.com" in lower:
        date_match = re.search(r'/wp-content/uploads/(\d{4})/(\d{2})/(\d{2})/', lower)
        if date_match:
            import datetime as _dt
            try:
                pub_date = _dt.datetime(int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3)),
                                        tzinfo=_dt.timezone.utc)
                age_days = (time.time() - pub_date.timestamp()) / 86400
                if age_days > 30:
                    return None
            except Exception:
                pass
        else:
            # No date in URL — check year only
            year_match = re.search(r'/wp-content/uploads/(\d{4})/', lower)
            if year_match and int(year_match.group(1)) < 2025:
                return None
    # Al Jazeera also serves small UI label/badge graphics (e.g. "Post-Label",
    # "Breaking-Label") that aren't photos at all, plus tiny fit= dimensions
    # that confirm a non-photo asset.
    if "aljazeera.com" in lower:
        if any(t in lower for t in ["post-label", "label-image", "badge-", "-label-"]):
            return None
        fit_match = re.search(r'fit=(\d+)%2c(\d+)', lower)
        if fit_match and (int(fit_match.group(1)) < 200 or int(fit_match.group(2)) < 200):
            return None
        resize_match = re.search(r'resize=(\d+)%2c(\d+)', lower)
        if resize_match and int(resize_match.group(1)) < 800:
            return None
        # Filenames with embedded dimensions (e.g. "image-1000x562.jpg") are
        # pre-sized graphic assets, not raw photos.
        if re.search(r'-\d+x\d+-\d+\.', lower):
            return None
    # France 24 filenames ending in -CS.jpg are broadcast graphics/composite
    # images with text/graphics overlaid — not clean news photos.
    if "france24.com" in lower and (re.search(r'-cs\d*\.jpg$', lower) or lower.endswith("-cs.jpg")):
        return None
    # France 24 PNG files are always graphics, not news photos.
    if "france24.com" in lower and (lower.endswith(".png") or ".png?" in lower):
        return None
    # Short broadcast-style filenames (EN-1.jpg, EN-1-1.jpg, FR-2.jpg etc)
    if "france24.com" in lower and re.search(r'/[a-z]{2,4}-\d+(-\d+)?\.jpg$', lower):
        return None
    if "france24.com" in lower and any(t in lower for t in ["img-default", "default-f24", "logo-f24", "placeholder", "reporters-", "/reporters/", "fr-en.jpg", "-fr-en-", "capture-", "anglais-", "/angl", "france-m%c3%a9dias", "france-medias", "-fmm-", "fmm-en", "fmm-fr", "fmm-ar", "1280x720px", "1280x720-", "1280x720_", "1920x1080px", "1920x1080-", "1920x1080_", "france24-", "minien-", "minifr-", "miniar-", "images-tiktok", "images-twitter", "images-facebook", "images-social", "vignette", "thumbnail", "montage-", "news_en", "news_fr", "news_ar"]):
        return None
    # Also catch filenames ending in -EN.jpg, -FR.jpg, -AR.jpg (broadcast language tags)
    if "france24.com" in lower and re.search(r'-(en|fr|ar)\.jpg$', lower):
        return None
    # France 24 URLs contain a /w:NNN/ width parameter — reject small sizes
    # and upgrade larger ones to 1280px for better quality.
    if "france24.com" in url:
        w_match = re.search(r'/w:(\d+)/', url)
        if w_match:
            w = int(w_match.group(1))
            if w < 400:
                return None
            # Upgrade to 1280 width for better quality
            url = re.sub(r'/w:\d+/', '/w:1280/', url)
            lower = url.lower()
    # Reject SVG and GIF files.
    if lower.endswith(".svg") or ".svg?" in lower:
        return None
    if lower.endswith(".gif") or ".gif?" in lower:
        return None
    if "ytimg.com" in lower or "youtube.com" in lower:
        return None
    # CBC: upgrade small Resize= values and reject tiny ones
    if "i.cbc.ca" in lower or "cbcrc.ca" in lower:
        resize_m = re.search(r'Resize%3D%28(\d+)%29', url) or re.search(r'Resize=\((\d+)\)', url)
        if resize_m:
            w = int(resize_m.group(1))
            if w < 800:
                # Try to upgrade to 1280
                url = re.sub(r'Resize%3D%28\d+%29', 'Resize%3D%281280%29', url)
                url = re.sub(r'Resize=\(\d+\)', 'Resize=(1280)', url)
                lower = url.lower()
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
    # Hard age cutoffs at collection time — images older than these thresholds
    # are rejected entirely so they never enter the pool.
    if "bbci.co.uk" in lower:
        age = bbc_uuid_age_days(url)
        if age is not None and age > 7:
            return None
    if "france24.com" in lower:
        age = bbc_uuid_age_days(url)  # France 24 also uses UUIDv1
        if age is not None and age > 7:
            return None
    if "i-scmp.com" in lower or "cdn.i-scmp.com" in lower:
        age = bbc_uuid_age_days(url)  # SCMP also uses UUIDv1 in filenames
        if age is not None and age > 14:
            return None
    if "spiegel.de" in lower and lower.endswith(".png"):
        return None
    # NPR brightspotcdn — filter out podcast, games, music, and quiz images
    # which are non-news assets; allow real news photos.
    if "brightspotcdn" in lower or "media.npr.org" in lower:
        if any(bad in lower for bad in [
            "games-we-love", "podcast", "music", "puzzle", "quiz", "crossword",
            "default-wide", "placeholder", "share-image", "shareimage",
        ]):
            return None
        # NPR .png files are usually graphics, not photos
        if lower.endswith(".png"):
            return None
        # Reject staff/byline headshots — square crops
        if "crop/" in lower and re.search(r'crop/(\d+)x\1', lower):
            return None
        # Reject forced-aspect distorted resizes (e.g. resize/1800x101!) — tiny height
        forced_resize = re.search(r'resize/(\d+)x(\d+)!', lower)
        if forced_resize and int(forced_resize.group(2)) < 200:
            return None
        # Reject absurdly large resize widths (>5000px) — malformed/broken URLs
        large_resize = re.search(r'resize/(\d+)x', lower)
        if large_resize and int(large_resize.group(1)) > 5000:
            return None
        # Upgrade resolution — handle both resize/NNN and resize/NNNxMMM formats
        url = re.sub(r'resize/\d+x\d+!?', 'resize/1400x788', url)
        url = re.sub(r'resize/\d+(?!x)', 'resize/1400', url)
    # BBC /images/ic/ URLs with programme IDs (p0...) are show/podcast assets, not news photos.
    if "bbci.co.uk/images/ic/" in lower and "/p0" in lower:
        return None
    # Guardian: reject portrait crops and near-full-frame crops.
    # Format: /hash/x_y_width_height/size.jpg
    if "media.guim.co.uk" in lower:
        m = re.search(r'/(\d+)_(\d+)_(\d+)_(\d+)/', urllib.parse.urlparse(url).path)
        if m:
            x, y, crop_w, crop_h = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            # Reject portrait crops.
            if crop_h > crop_w:
                return None
            # Reject near-full-frame crops (offset under 100px in both axes) —
            # these tend to be stock/archival/generic photos rather than
            # scene-specific news photography.
            if x < 100 and y < 100:
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
        # Skip France 24 French national news section
        if "france24.com" in lower and "/en/france/" in lower:
            continue
        # Skip Al Jazeera tech/science/sport/culture sections
        if "aljazeera.com" in lower and any(s in lower for s in [
            "/technology/", "/science/", "/sport/", "/arts-and-culture/",
            "/economy/", "/climate-crisis/", "/features/",
        ]):
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
            if not any(token in lower for token in ["ichef.bbci", "guardian", "aljazeera", "france24", "brightspotcdn", "cgtn", "i-scmp", "mexiconewsdaily", "static.dw", "i.cbc"]):
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
            "media.guim.co.uk",
            "i.guim.co.uk",
            "aljazeera.net",
            "aljazeera.com",
            "s.france24.com",
            "news.cgtn.com",
            "img.cgtn.com",
            "img.i-scmp.com",
            "cdn.i-scmp.com",
            "mexiconewsdaily.com",
            "static.dw.com",
            "i.cbc.ca",
            "cbcrc.ca",
            "npr.brightspotcdn.com",
            ".jpg",
            ".jpeg",
            ".webp",
        ]):
            return False
        # Guardian author avatars and small images.
        if "yimg.com" in lower and (";w=80;" in lower or ";h=60;" in lower or "logo" in lower):
            return False
        if "static.theguardian.com" in lower:
            return False
        if "jobs.theguardian.com" in lower:
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
        r'https://media\.guim\.co\.uk/[^"\'\s<>]+',
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
    if "guim.co.uk" in lower or "theguardian" in lower:
        return "guardian"
    if "aljazeera" in lower or "france24" in lower or "brightspotcdn" in lower or "cgtn" in lower or "i-scmp" in lower or "scmp" in lower or "mexiconewsdaily" in lower or "static.dw" in lower or "i.cbc" in lower:
        return "other"
    if "bbci.co.uk" in lower or "bbc.co.uk" in lower:
        return "bbc"
    return "other"


def bbc_uuid_age_days(url):
    """Decode a BBC UUIDv1 image URL and return the image's age in days.
    Returns None if the URL doesn't contain a decodable UUID.
    BBC image filenames contain UUIDv1 values whose time fields encode a
    60-bit timestamp in 100-nanosecond intervals since Oct 15, 1582.
    """
    m = re.search(r'([0-9a-f]{8})-([0-9a-f]{4})-([0-9a-f]{4})-', url)
    if not m:
        return None
    try:
        time_low = int(m.group(1), 16)
        time_mid = int(m.group(2), 16)
        time_hi  = int(m.group(3), 16) & 0x0FFF
        timestamp_100ns = (time_hi << 48) | (time_mid << 32) | time_low
        uuid_epoch_offset = 0x01b21dd213814000
        unix_seconds = (timestamp_100ns - uuid_epoch_offset) / 1e7
        return (time.time() - unix_seconds) / 86400
    except Exception:
        return None


def weighted_image_mix(images, limit=MAX_IMAGE_POOL):
    """Sort images by freshness (newest first) with a max-2-consecutive-per-source
    rule to ensure variety even when one source dominates the fresh content.
    """
    import datetime as _dt

    def image_source(url):
        lower = url.lower()
        if "bbci.co.uk" in lower or "bbc.co.uk" in lower: return "bbc"
        if "guim.co.uk" in lower or "theguardian" in lower: return "guardian"
        if "aljazeera" in lower: return "aljazeera"
        if "france24" in lower: return "france24"
        if "i-scmp" in lower or "scmp" in lower: return "scmp"
        if "i.cbc.ca" in lower or "cbcrc" in lower: return "cbc"
        return "other"

    def image_timestamp(url):
        # BBC, France 24, SCMP — exact UUIDv1 timestamp
        age_days = bbc_uuid_age_days(url)
        if age_days is not None:
            return time.time() - (age_days * 86400)
        # Guardian — use API date if available (covers both API and some scraped)
        if url in GUARDIAN_IMAGE_DATE:
            try:
                return _dt.datetime.strptime(GUARDIAN_IMAGE_DATE[url][:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc).timestamp()
            except Exception:
                pass
        lower = url.lower()
        # Al Jazeera — extract upload date from WordPress URL path (/2026/06/22/)
        if "aljazeera" in lower:
            m = re.search(r'/wp-content/uploads/(\d{4})/(\d{2})/(\d{2})/', lower)
            if m:
                try:
                    pub_ts = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                         tzinfo=_dt.timezone.utc).timestamp()
                    return pub_ts
                except Exception:
                    pass
            return time.time() - 3600  # fallback: assume 1hr old
        # CBC — no timestamp signal, assume moderately fresh
        if "i.cbc.ca" in lower:
            return time.time() - 3600
        # Guardian scraped (no API date) — assume same-day but not brand new
        if "guim.co.uk" in lower:
            return time.time() - 7200
        return time.time() - 7200

    # Deduplicate
    seen = set()
    deduped = []
    for img in images:
        key = canonical_image_key(img)
        if key not in seen:
            seen.add(key)
            deduped.append(img)

    # Sort globally by freshness
    sorted_imgs = sorted(deduped, key=image_timestamp, reverse=True)

    # Re-order so no more than 2 consecutive images are from the same source.
    result = []
    remaining = list(sorted_imgs)
    last_sources = []

    while remaining and len(result) < limit:
        placed = False
        for i, img in enumerate(remaining):
            src = image_source(img)
            if last_sources[-2:].count(src) < 2:
                result.append(img)
                last_sources.append(src)
                remaining.pop(i)
                placed = True
                break
        if not placed:
            # All remaining are same source — just take next
            result.append(remaining.pop(0))
            last_sources.append(image_source(result[-1]))

    return result


def get_bbc_images(limit=MAX_IMAGE_POOL):
    now = time.time()
    with IMAGE_CACHE["lock"]:
        if IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
            cached = IMAGE_CACHE["images"][:]
            return cached[:limit]

    images = []
    seen = set()
    non_bbc_article_scrape_budget = 60
    bbc_added = 0
    page_article_scrape_budget = 50
    max_bbc_images = int(limit * 0.30)

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
            is_aljazeera_feed = "aljazeera.com" in feed_url
            for item in items[:item_limit]:
                # Skip BBC items from sections we don't want
                if is_bbc:
                    link_el = item.find("link")
                    link_url = (link_el.text or "") if link_el is not None else ""
                    if any(s in link_url for s in [
                        "/technology/", "/tech/", "/sport/", "/entertainment/",
                        "/culture/", "/travel/", "/food/", "/science/",
                    ]):
                        continue
                # Skip Al Jazeera tech/science/sport/culture items
                if is_aljazeera_feed:
                    link_el = item.find("link")
                    link_url = (link_el.text or "") if link_el is not None else ""
                    if any(s in link_url for s in [
                        "/technology/", "/science/", "/sport/", "/arts-and-culture/",
                        "/economy/", "/climate-crisis/",
                    ]):
                        continue
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

    # Sort each source by feed order (newest first), then interleave with weighted mix.
    guardian_imgs = [img for img in images if source_category(img) == "guardian"]
    bbc_imgs = [img for img in images if source_category(img) == "bbc"]
    other_imgs = [img for img in images if source_category(img) not in ("guardian", "bbc")]
    ordered_by_source = guardian_imgs + other_imgs + bbc_imgs
    ordered = weighted_image_mix(ordered_by_source, limit=limit)

    with IMAGE_CACHE["lock"]:
        IMAGE_CACHE["time"] = now
        IMAGE_CACHE["images"] = ordered[:]
    return ordered[:limit]


def _fast_startup_seed():
    """Quickly populate cache with BBC images so the page isn't black on first load."""
    print("[STARTUP] Fast seeding from BBC RSS …", flush=True)
    images = []
    seen = set()
    try:
        for feed_url in ["https://feeds.bbci.co.uk/news/world/rss.xml",
                         "https://feeds.bbci.co.uk/news/rss.xml"]:
            try:
                rss = fetch_text(feed_url, timeout=4.0)
                root = ET.fromstring(rss)
                for item in root.findall(".//item")[:30]:
                    img = extract_rss_item_image(item)
                    if not img:
                        continue
                    cleaned = clean_extracted_image_url(img)
                    if not cleaned or url_is_known_bad(cleaned):
                        continue
                    key = canonical_image_key(cleaned)
                    if key in seen:
                        continue
                    seen.add(key)
                    images.append(cleaned)
            except Exception as e:
                print(f"[STARTUP] Feed error: {e}", flush=True)
    except Exception as e:
        print(f"[STARTUP] Seed error: {e}", flush=True)
    if images:
        with IMAGE_CACHE["lock"]:
            IMAGE_CACHE["images"] = images
            IMAGE_CACHE["time"] = time.time() - CACHE_SECONDS + 30  # expire soon so full build replaces it
        print(f"[STARTUP] Seeded {len(images)} BBC images for fast first load", flush=True)


def _background_pool_refresher():
    """Continuously rebuild the image pool so the cache is always warm."""
    refresh_count = 0
    while True:
        try:
            print("[BG] Refreshing image pool …", flush=True)
            t0 = time.time()
            if refresh_count % 10 == 0:
                APPROVED_URLS.clear()
                APPROVED_VERTICAL_URLS.clear()
                print("[BG] Cleared approved URLs for fresh vet", flush=True)
            # Clear REJECT_CACHE before rebuilding the pool so a stale rejection
            # from a previous cycle doesn't block an image from being collected
            # this cycle (it would otherwise sit unused until the cache aged out).
            REJECT_CACHE.clear()
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

    # AP's CDN rate-limits aggressively (429s) when many images are fetched
    # in a short window — this happens when many users load the page at once,
    # since every AP image is fetched live with no pre-warmed cache. Slowly
    # pre-cache a batch of AP images here, one at a time with a short delay,
    # so more of them are already cached by the time users request them.
    ap_seed = [img for img in images
               if "dims.apnews.com" in img
               and img not in PROXY_CACHE
               and not url_is_known_bad(img)][:15]
    ap_cached = 0
    for url in ap_seed:
        try:
            data, content_type = fetch_bytes(url, timeout=8)
            if content_type.startswith("image/") and len(data) > 10000:
                PROXY_CACHE[url] = {"time": time.time(), "data": data, "content_type": content_type}
                APPROVED_URLS.add(url)
                ap_cached += 1
        except Exception:
            pass
        time.sleep(2.0)  # spread requests out to avoid tripping AP's rate limiter
    if ap_seed:
        print(f"[BG] AP pre-cached: {ap_cached}/{len(ap_seed)} succeeded.", flush=True)


def _pre_vet_one(url):
    """Fetch and check one image; add to APPROVED_URLS if it passes."""
    if url in APPROVED_URLS:
        return
    is_guardian = "guim.co.uk" in url or "theguardian.com" in url
    is_bbc = "bbci.co.uk" in url or "bbc.co.uk" in url
    is_aljazeera = "aljazeera" in url.lower()
    is_scmp = "i-scmp.com" in url.lower() or "scmp.com" in url.lower()
    is_france24 = "france24.com" in url.lower()
    is_cbc = "i.cbc.ca" in url.lower() or "cbcrc.ca" in url.lower()
    if not is_guardian and not is_bbc and not is_aljazeera and not is_scmp and not is_france24 and not is_cbc:
        # Other sources are auto-approved —
        # vertical/ratio checking happens at proxy-serve time.
        APPROVED_URLS.add(url)
        return
    if is_aljazeera or is_scmp or is_france24 or is_cbc:
        # Run graphic-page detection only (no portrait/divider checks).
        try:
            data, content_type = fetch_bytes(url, timeout=8)
            if not content_type.startswith("image/"):
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
            # Fetch failed (CDN blocking pre-vet) — don't auto-approve.
            # Leave unapproved so the proxy-time cv2 check still runs.
            pass
        return
    # Guardian and BBC — run full cv2 checks.
    try:
        data, content_type = fetch_bytes(url, timeout=8)
        if not content_type.startswith("image/"):
            REJECT_CACHE[url] = {"time": time.time()}
            if is_bbc:
                print(f"[VET] BBC rejected (not image, content_type={content_type}): {url[:90]}", flush=True)
            return
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            REJECT_CACHE[url] = {"time": time.time()}
            if is_bbc:
                print(f"[VET] BBC rejected (decode failed): {url[:90]}", flush=True)
            return
        ih, iw = img.shape[:2]
        min_w = MIN_IMAGE_WIDTH
        min_h = MIN_IMAGE_HEIGHT
        if iw < MIN_IMAGE_WIDTH or ih < MIN_IMAGE_HEIGHT:
            REJECT_CACHE[url] = {"time": time.time()}
            if is_bbc:
                print(f"[VET] BBC rejected (too small {iw}x{ih}): {url[:90]}", flush=True)
            return
        if ih > iw * 1.4:
            # Moderate verticals (up to ~2.2:1) are kept for mobile use instead
            # of being discarded — extreme/banner-like crops are still rejected.
            if ih > iw * 2.2 or iw < min_w * 0.5:
                REJECT_CACHE[url] = {"time": time.time()}
                if is_bbc:
                    print(f"[VET] BBC rejected (extreme vertical {iw}x{ih}): {url[:90]}", flush=True)
                return
            APPROVED_VERTICAL_URLS.add(url)
        # The isolated-subject/portrait check is tuned for Guardian's stock-photo
        # patterns and was rejecting too many legitimate BBC news photos — skip
        # it for BBC and rely on the graphic-page and center-divider checks,
        # which catch the actual complaints (logos, infographics, split images).
        if not is_bbc and image_is_portrait_or_generic_isolated_subject(data, allow_vertical=(ih > iw * 1.4)):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        # Re-enabled for BBC with strict=True — uses higher thresholds to avoid
        # false positives on blue sky / white clothing in real outdoor photos.
        if image_is_probably_full_graphic_page(data, strict=is_bbc):
            REJECT_CACHE[url] = {"time": time.time()}
            return
        if image_has_center_divider(data):
            REJECT_CACHE[url] = {"time": time.time()}
            if is_bbc:
                print(f"[VET] BBC rejected (center divider): {url[:90]}", flush=True)
            return
        APPROVED_URLS.add(url)
        if is_bbc:
            print(f"[VET] BBC approved: {url[:90]}", flush=True)
    except Exception as e:
        if is_bbc:
            print(f"[VET] BBC fetch/exception ({e}): {url[:90]}", flush=True)
        pass


def _pre_vet_pool(images):
    """Pre-vet all images in the pool in the background using a thread pool."""
    REJECT_CACHE.clear()
    to_vet = [u for u in images if u not in APPROVED_URLS]
    print(f"[BG] Pre-vetting {len(to_vet)} images …", flush=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_pre_vet_one, to_vet))
    print(f"[BG] Pre-vet done. Approved: {len(APPROVED_URLS)}", flush=True)


def image_is_probably_full_graphic_page(data, strict=False):
    """Detect full-page infographics/charts. strict=True uses higher thresholds
    for sources (like BBC) where outdoor photos with blue sky commonly trigger
    the standard blue/white detection."""
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
    if strict:
        # Stricter thresholds for BBC — require larger fractions and all three
        # channels together, so blue sky + white clouds doesn't trigger.
        if blue_frac > 0.35 and white_frac > 0.08 and edge_density > 0.06:
            return True
        if green_frac > 0.05 and white_frac > 0.05 and edge_density > 0.08:
            return True
        if unique_colors < 600 and edge_density > 0.09 and gray_std < 50:
            return True
        if gray_std < 28 and edge_density > 0.09:
            return True
        # BBC branded graphics use a distinctive red/crimson color — detect it.
        # BBC red is approximately H:0-10, S>150, V>100 in HSV.
        red_mask = ((hsv[:, :, 0] < 10) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 100)
        red_frac = float(np.mean(red_mask))
        if red_frac > 0.04 and white_frac > 0.05:
            return True
    else:
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


def measure_bbc_branding_height(img):
    """Find the actual pixel height of the BBC branding bar by scanning rows
    from the top and finding where the blue/green/white branding band ends."""
    try:
        h, w = img.shape[:2]
        scan_h = max(1, int(h * 0.42))
        top = img[:scan_h, :]
        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)
        blue_mask = (hsv[:, :, 0] > 98) & (hsv[:, :, 0] < 138) & (hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 40)
        green_mask = (hsv[:, :, 0] > 42) & (hsv[:, :, 0] < 92) & (hsv[:, :, 1] > 75) & (hsv[:, :, 2] > 50)
        white_mask = gray > 210
        branding_mask = blue_mask | green_mask | white_mask
        row_frac = np.mean(branding_mask, axis=1)
        # Walk down from the top; the branding bar is a contiguous band of
        # high branding-color rows. Stop at the first row that drops below
        # threshold for several consecutive rows (real photo content starts).
        threshold = 0.35
        consecutive_low = 0
        last_branding_row = 0
        for i, frac in enumerate(row_frac):
            if frac >= threshold:
                last_branding_row = i
                consecutive_low = 0
            else:
                consecutive_low += 1
                if consecutive_low >= 6:
                    break
        # Add a small margin below the detected bar, but cap how much we
        # ever crop so we don't eat into faces lower in the frame.
        crop_px = min(last_branding_row + 8, int(h * 0.16))
        return max(0, crop_px)
    except Exception:
        return 0


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
        crop_px = measure_bbc_branding_height(img)
        if crop_px <= 0:
            return img, False
        cropped = img[crop_px:, :]
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


def image_is_portrait_or_generic_isolated_subject(data, allow_vertical=False):
    """
    Reject images that read like a single cut-out/portrait rather than a news scene.

    This catches:
    - centered single faces / headshots
    - cropped isolated people/objects on smooth generic backgrounds
    - vertical/cropped editorial photos that survive URL rules (unless allow_vertical)
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

    # Verticals are normally excluded from the main pool, but moderate vertical
    # crops (up to ~2.2:1) are kept when allow_vertical is set, for mobile use.
    if h > w * 1.05 and not allow_vertical:
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
    # Require near-pure white/black (not just "bright sky" or "dark shadow")
    # and a much taller, more consistent run — real divider lines/borders are
    # stark and span nearly the full height; ordinary photo content (sky,
    # water, shadow) rarely does both at once.
    bright = gray > 248
    dark = gray < 8
    center_min = int(w * 0.18)
    center_max = int(w * 0.82)
    for x in range(center_min, center_max):
        bright_band = bright[:, max(0, x - 1):min(w, x + 2)]
        dark_band = dark[:, max(0, x - 1):min(w, x + 2)]
        bright_by_row = np.mean(bright_band, axis=1) > 0.7
        dark_by_row = np.mean(dark_band, axis=1) > 0.7
        for line_by_row in (bright_by_row, dark_by_row):
            full_height_frac = float(np.mean(line_by_row))
            if full_height_frac > 0.88:
                return True
            if full_height_frac > 0.75:
                transitions = np.diff(line_by_row.astype(np.int8))
                if int(np.sum(transitions == 1)) <= 2:
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
    strong_cols = np.sum(center_energy > baseline * 3.4)
    if strong_cols >= 2:
        strong_col_indices = np.where(center_energy > baseline * 3.4)[0]
        for ci in strong_col_indices:
            abs_ci = center_min + ci
            col_slice = edge_strength[:, max(0, abs_ci - 1):min(w, abs_ci + 2)]
            row_strength = col_slice.mean(axis=1)
            row_baseline = float(np.median(row_strength)) + 1e-6
            strong_frac = float(np.mean(row_strength > row_baseline * 1.8))
            if strong_frac > 0.70:
                return True
    divider_x = center_min + int(np.argmax(center_energy))
    peak_energy = float(col_energy[divider_x])
    if peak_energy < baseline * 3.4:
        return False
    col_slice = edge_strength[:, max(0, divider_x - 1):min(w, divider_x + 2)]
    row_strength = col_slice.mean(axis=1)
    row_baseline = float(np.median(row_strength)) + 1e-6
    strong_frac = float(np.mean(row_strength > row_baseline * 1.8))
    return strong_frac > 0.65


def render_html():
    with IMAGE_CACHE["lock"]:
        cached = IMAGE_CACHE["images"][:]
    clean = [img for img in cached
             if not url_is_known_bad(img)
             and img not in REJECT_CACHE]

    def image_age_seconds(url):
        """Return estimated age in seconds. Lower = newer."""
        import datetime as _dt
        age_days = bbc_uuid_age_days(url)
        if age_days is not None:
            return age_days * 86400
        if url in GUARDIAN_IMAGE_DATE:
            try:
                pub_ts = _dt.datetime.strptime(GUARDIAN_IMAGE_DATE[url][:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=_dt.timezone.utc).timestamp()
                return time.time() - pub_ts
            except Exception:
                pass
        lower = url.lower()
        if "aljazeera" in lower:
            m = re.search(r'/wp-content/uploads/(\d{4})/(\d{2})/(\d{2})/', lower)
            if m:
                try:
                    pub_ts = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                         tzinfo=_dt.timezone.utc).timestamp()
                    return time.time() - pub_ts
                except Exception:
                    pass
            return 3600
        if "i.cbc.ca" in lower:
            return 3600
        if "guim.co.uk" in lower:
            return 7200
        return 7200

    # Take top candidates from the pool and sort by actual freshness
    candidates = clean[:60]
    seed = sorted(candidates, key=image_age_seconds)[:10]
    sequence = []
    for img in seed:
        src = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({"src": src, "raw": img, "verticalOnly": url_is_vertical_only(img) or img in APPROVED_VERTICAL_URLS})
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
    if (!currentImage) {{ loadRandomSlide(); setTimeout(tryLoad, currentImage ? 1000 : 300); }}
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
                const insertAt = Math.min(poolIndex + 1, shuffledPool.length);
                shuffledPool.splice(insertAt, 0, ...shuffleArray(newSrcs));
              }}
              if (!currentImage) loadRandomSlide();
            }}
            // Poll fast until we have enough images, then slow down.
            const hasEnough = slides.length > 50;
            setTimeout(refresh, hasEnough ? 15000 : currentImage ? 3000 : 500);
          }} else {{
            setTimeout(refresh, currentImage ? 2000 : 500);
          }}
        }} else {{ setTimeout(refresh, currentImage ? 2000 : 500); }}
      }} catch(e) {{ setTimeout(refresh, currentImage ? 2000 : 500); }}
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
  if (src.includes('dims.apnews.com')) return 1;
  if (src.includes('bbci.co.uk')) return 1;
  if (src.includes('aljazeera')) return 2;
  return 2;
}}
let _refillCount = 0;
function refillPool() {{
  _refillCount++;
  // Periodically clear badSrcs so transient failures (slow proxy, brief
  // network hiccup) don't permanently shrink the usable pool over time.
  if (_refillCount % 8 === 0 && badSrcs.size > 0) {{
    badSrcs.clear();
  }}

  let candidates = slides
    .filter(slideAllowedForCurrentOrientation)
    .map(s => s.src)
    .filter(src => !badSrcs.has(src));

  if (candidates.length < 20 && badSrcs.size > 0) {{
    badSrcs.clear();
    candidates = slides.filter(slideAllowedForCurrentOrientation).map(s => s.src);
  }}

  candidates = [...new Set(candidates)];

  let fresh = candidates.filter(src => !_shownThisCycle.has(src));
  // Only reset when genuinely exhausted — don't reset early or images repeat.
  if (fresh.length < 3) {{
    _shownThisCycle.clear();
    fresh = candidates;
  }}

  // Server already interleaves sources and orders by relevance — just shuffle
  // the whole fresh set rather than re-segregating by source.
  shuffledPool = shuffleArray(fresh);
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
  const radius = minDim * (isTouchDevice ? 0.24 : 0.18);
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

  // Safety valve — if the image neither loads nor errors within 11s, move on.
  // The server's own proxy fetch has an 8s timeout, so this needs headroom
  // above that or slow-loading images (e.g. BBC on a cold cache) get
  // blacklisted client-side before the server even finishes fetching them.
  let timeout = setTimeout(() => {{
    isLoadingSlide = false;
    badSrcs.add(src);
    loadRandomSlide(attempts + 1);
  }}, 11000);

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
let _canvasRect = null;
window.addEventListener("resize", () => {{ _canvasRect = null; }});

function updateFlashlightPositionFromPointer(e) {{
  if (!_canvasRect) _canvasRect = canvas.getBoundingClientRect();
  const isTouchDevice = window.matchMedia("(pointer: coarse)").matches;
  const offsetY = isTouchDevice ? window.innerHeight * 0.12 : 0;
  mouseX = (e.clientX - _canvasRect.left) * DPR;
  mouseY = ((e.clientY - _canvasRect.top) - offsetY) * DPR;
  if (!_rafPending) {{ _rafPending = true; requestAnimationFrame(() => {{ _rafPending = false; drawFlashlight(); }}); }}
}}
canvas.addEventListener("pointermove", updateFlashlightPositionFromPointer, {{ passive: true }});
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
                sequence.append({"src": src, "raw": img, "verticalOnly": url_is_vertical_only(img) or img in APPROVED_VERTICAL_URLS})
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
            counts = {"bbc": 0, "guardian": 0, "aljazeera": 0, "france24": 0, "cgtn": 0, "scmp": 0, "dw": 0, "cbc": 0, "other": 0}
            for img in images:
                lower = img.lower()
                if "bbci.co.uk" in lower:
                    counts["bbc"] += 1
                elif "guim.co.uk" in lower or "theguardian" in lower:
                    counts["guardian"] += 1
                elif "aljazeera" in lower:
                    counts["aljazeera"] += 1
                elif "france24" in lower:
                    counts["france24"] += 1
                elif "cgtn" in lower:
                    counts["cgtn"] += 1
                elif "i-scmp" in lower or "scmp" in lower:
                    counts["scmp"] += 1
                elif "static.dw" in lower:
                    counts["dw"] += 1
                elif "i.cbc" in lower or "cbcrc" in lower:
                    counts["cbc"] += 1
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
                # But still run a lightweight ratio check for non-Guardian/BBC
                # sources (e.g. Al Jazeera) that were auto-approved without
                # dimension checking, so verticals get properly tagged.
                if url in APPROVED_URLS:
                    is_guardian = "guim.co.uk" in url or "theguardian.com" in url
                    is_bbc = "bbci.co.uk" in url or "bbc.co.uk" in url
                    is_scmp = "i-scmp" in url.lower()
                    is_f24 = "france24" in url.lower()
                    if not is_guardian and not is_bbc and url not in APPROVED_VERTICAL_URLS:
                        try:
                            arr = np.frombuffer(data, np.uint8)
                            img_check = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if img_check is not None:
                                ih, iw = img_check.shape[:2]
                                if ih > iw * 1.4:
                                    if ih > iw * 2.2:
                                        REJECT_CACHE[url] = {"time": time.time()}
                                        self.safe_send_bytes(415, b"Rejected portrait", extra_headers={"Cache-Control": "no-store"})
                                        return
                                    APPROVED_VERTICAL_URLS.add(url)
                                # France 24 and BBC: run graphic and divider checks on approved images too
                                if is_f24 or is_bbc:
                                    if image_is_probably_full_graphic_page(data, strict=is_bbc) or image_has_center_divider(data):
                                        REJECT_CACHE[url] = {"time": time.time()}
                                        self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                                        return
                                # For SCMP, also run graphic, sharpness, and illustration checks
                                if is_scmp:
                                    if image_is_probably_full_graphic_page(data) or image_has_center_divider(data):
                                        REJECT_CACHE[url] = {"time": time.time()}
                                        self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                                        return
                                    gray = cv2.cvtColor(img_check, cv2.COLOR_BGR2GRAY)
                                    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                                    if lap < 80:
                                        REJECT_CACHE[url] = {"time": time.time()}
                                        self.safe_send_bytes(415, b"Rejected low sharpness", extra_headers={"Cache-Control": "no-store"})
                                        return
                                    # Illustration/cartoon detection — sample at 200x112 for better
                                    # color palette accuracy. Real photos have thousands of colors;
                                    # drawings/cartoons use a limited palette.
                                    sample = cv2.resize(img_check, (200, 112), interpolation=cv2.INTER_AREA)
                                    unique_colors = len(np.unique(sample.reshape(-1, 3), axis=0))
                                    if unique_colors < 1200:
                                        REJECT_CACHE[url] = {"time": time.time()}
                                        self.safe_send_bytes(415, b"Rejected illustration", extra_headers={"Cache-Control": "no-store"})
                                        return
                        except Exception:
                            pass
                    PROXY_CACHE[url] = {"time": time.time(), "data": data, "content_type": content_type}
                    self.safe_send_bytes(200, data, content_type, {"Cache-Control": "public, max-age=300"})
                    return

                # Not yet vetted — size check + graphic detection for known sources.
                try:
                    arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if img is not None:
                        ih, iw = img.shape[:2]
                        # Higher resolution floor for NPR — their images are often soft.
                        min_w = MIN_IMAGE_WIDTH
                        min_h = MIN_IMAGE_HEIGHT
                        if iw < MIN_IMAGE_WIDTH or ih < MIN_IMAGE_HEIGHT:
                            REJECT_CACHE[url] = {"time": time.time()}
                            self.safe_send_bytes(415, b"Rejected low resolution image", extra_headers={"Cache-Control": "no-store"})
                            return
                        if ih > iw * 1.4:
                            if ih > iw * 2.2 or iw < min_w * 0.5:
                                REJECT_CACHE[url] = {"time": time.time()}
                                self.safe_send_bytes(415, b"Rejected portrait", extra_headers={"Cache-Control": "no-store"})
                                return
                            APPROVED_VERTICAL_URLS.add(url)
                        # Run graphic-page detection for sources whose pre-vet fetch
                        # may have failed (Al Jazeera, SCMP, France24, BBC CDN blocks).
                        _is_aj = "aljazeera" in url.lower()
                        _is_scmp = "i-scmp" in url.lower()
                        _is_f24 = "france24" in url.lower()
                        _is_cbc = "i.cbc.ca" in url.lower()
                        _is_bbc_unvet = "bbci.co.uk" in url.lower()
                        if _is_aj or _is_scmp or _is_f24 or _is_cbc or _is_bbc_unvet:
                            if image_is_probably_full_graphic_page(data, strict=_is_bbc_unvet) or image_has_center_divider(data):
                                REJECT_CACHE[url] = {"time": time.time()}
                                self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                                return
                            # Sharpness check for SCMP — reject genuinely blurry/grainy images
                            # (some SCMP images are low-quality wire photos upscaled to 1280x720)
                            if _is_scmp:
                                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                                laplacian_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                                if laplacian_var < 80:
                                    REJECT_CACHE[url] = {"time": time.time()}
                                    self.safe_send_bytes(415, b"Rejected low sharpness", extra_headers={"Cache-Control": "no-store"})
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
                print("[FETCH FAILED]", url[:80], e)
                self.safe_send_bytes(502, b"Image fetch failed")
                return
        self.safe_send_bytes(404, b"Not found")


if __name__ == "__main__":
    REJECT_CACHE.clear()
    APPROVED_URLS.clear()
    APPROVED_VERTICAL_URLS.clear()
    _load_guardian_cache_from_disk()
    print()
    print("misshurry")
    print("RSS + AP/Reuters/Guardian/NPR image pool: ON")
    print("Low-res rejection: ON")
    print(f"Serving at http://localhost:{PORT}")
    print()
    bg = threading.Thread(target=_background_pool_refresher, daemon=True)
    bg.start()
    # Fast seed so the page isn't black while the full pool builds.
    seed_thread = threading.Thread(target=_fast_startup_seed, daemon=True)
    seed_thread.start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
