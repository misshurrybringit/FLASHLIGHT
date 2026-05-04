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
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml",
    "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
    "https://feeds.bbci.co.uk/news/world/latin_america/rss.xml",
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/health/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://feeds.bbci.co.uk/news/in_pictures/rss.xml",
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

MAX_IMAGE_POOL = 1400
SEQUENCE_LENGTH = 1200
AUTO_ADVANCE_MS = 5000

IMAGE_CACHE = {"time": 0, "images": []}
CACHE_SECONDS = 35

PROXY_CACHE = {}
PROXY_CACHE_SECONDS = 300
PROXY_CACHE_MAX_ITEMS = 420

REJECT_CACHE = {}
REJECT_CACHE_SECONDS = 1800

# Hard rejects: images/cards you never want anywhere.
KNOWN_BAD_URL_FRAGMENTS = [
    "p0l7jnbt",
    "p0kxxp17",
    "p0n9y769",
    "3a08bc10",
    "c5a74450",
    "f53b6250",
    "p0ngd4cc",
    "acb55400",
]

# These are not rejected; they are allowed only on vertical phone orientation.
# This catches cropped/tall editorial images that look bad on computer/landscape.
VERTICAL_ONLY_URL_FRAGMENTS = [
    "166137e0",
    "3600d2f0",
]

# Special-case light crop for top-left VOICE logo.
VOICE_TOP_CROP_FRAGMENTS = [
    "f16b6b80",
]


def url_matches_any(url, fragments):
    return any(fragment in url for fragment in fragments)


def url_is_known_bad(url):
    return url_matches_any(url, KNOWN_BAD_URL_FRAGMENTS)


def url_is_vertical_only(url):
    return url_matches_any(url, VERTICAL_ONLY_URL_FRAGMENTS)


def url_needs_voice_crop(url):
    return url_matches_any(url, VOICE_TOP_CROP_FRAGMENTS)


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


def fetch_text(url, timeout=4):
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


def extract_rss_item_image(item):
    media_ns = {"media": "http://search.yahoo.com/mrss/"}

    thumb = item.find("media:thumbnail", media_ns)
    if thumb is not None:
        return upgrade_bbc_image_url(thumb.attrib.get("url"))

    for media_content in item.findall("media:content", media_ns):
        url = media_content.attrib.get("url")
        mime = media_content.attrib.get("type", "")
        medium = media_content.attrib.get("medium", "")
        if url and (mime.startswith("image/") or medium == "image"):
            return upgrade_bbc_image_url(url)

    enclosure = item.find("enclosure")
    if enclosure is not None:
        url = enclosure.attrib.get("url", "")
        mime = enclosure.attrib.get("type", "")
        if url and mime.startswith("image/"):
            return upgrade_bbc_image_url(url)

    description = item.find("description")
    if description is not None and description.text:
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description.text, re.IGNORECASE)
        if m:
            return upgrade_bbc_image_url(m.group(1))

    return None


def get_bbc_images(limit=MAX_IMAGE_POOL, force_refresh=False):
    now = time.time()

    if (not force_refresh) and IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
        cached = IMAGE_CACHE["images"][:]
        random.shuffle(cached)
        return cached[:limit]

    images = []
    seen = set()
    feeds = RSS_FEEDS[:]
    random.shuffle(feeds)

    for feed_url in feeds:
        try:
            rss = fetch_text(feed_url, timeout=4)
            root = ET.fromstring(rss)
            items = root.findall(".//item")
            random.shuffle(items)

            for item in items[:900]:
                if len(images) >= limit:
                    break

                img = extract_rss_item_image(item)
                if not img:
                    continue

                if url_is_known_bad(img):
                    continue

                if img in seen:
                    continue

                rejected = REJECT_CACHE.get(img)
                if rejected and now - rejected["time"] < REJECT_CACHE_SECONDS:
                    continue

                seen.add(img)
                images.append(img)

        except Exception:
            continue

    random.shuffle(images)
    IMAGE_CACHE["time"] = now
    IMAGE_CACHE["images"] = images[:]

    return images[:limit]


def build_slide_sequence(force_refresh=False):
    images = get_bbc_images(limit=MAX_IMAGE_POOL, force_refresh=force_refresh)
    random.shuffle(images)

    sequence = []
    for img in images:
        proxied = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({
            "src": proxied,
            "verticalOnly": url_is_vertical_only(img),
        })

    return sequence


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

    small = cv2.resize(
        img,
        (100, max(56, int(img.shape[0] * 100 / img.shape[1]))),
        interpolation=cv2.INTER_AREA,
    )
    unique_colors = len(np.unique(small.reshape(-1, 3), axis=0))

    blue_mask = (
        (hsv[:, :, 0] > 98)
        & (hsv[:, :, 0] < 138)
        & (hsv[:, :, 1] > 80)
        & (hsv[:, :, 2] > 45)
    )
    green_mask = (
        (hsv[:, :, 0] > 42)
        & (hsv[:, :, 0] < 90)
        & (hsv[:, :, 1] > 85)
        & (hsv[:, :, 2] > 55)
    )
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
        top_h = max(1, int(h * 0.42))
        top = img[:top_h, :]

        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(top, cv2.COLOR_BGR2GRAY)

        blue_mask = (
            (hsv[:, :, 0] > 98)
            & (hsv[:, :, 0] < 138)
            & (hsv[:, :, 1] > 70)
            & (hsv[:, :, 2] > 40)
        )
        green_mask = (
            (hsv[:, :, 0] > 42)
            & (hsv[:, :, 0] < 92)
            & (hsv[:, :, 1] > 75)
            & (hsv[:, :, 2] > 50)
        )
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
            crop_y = int(h * 0.11)
            cropped = img[crop_y:, :]
            if cropped is not None and cropped.size > 0:
                return cropped, True

        if not top_has_bbc_branding(img):
            return img, False

        crop_y = int(h * 0.24)
        cropped = img[crop_y:, :]
        if cropped is None or cropped.size == 0:
            return img, False

        return cropped, True

    except Exception:
        return img, False


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

    # Bright thin vertical seams anywhere in the middle 80% of the image.
    white = gray > 218
    col_white = white.mean(axis=0)
    middle_min = int(w * 0.10)
    middle_max = int(w * 0.90)
    if middle_max > middle_min:
        for x in range(middle_min, middle_max):
            band = white[:, max(0, x - 1):min(w, x + 2)]
            if band.size and float(np.mean(band)) > 0.26:
                return True

    grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    edge_strength = np.abs(grad_x)
    col_energy = edge_strength.mean(axis=0)

    center_min = int(w * 0.18)
    center_max = int(w * 0.82)
    center_energy = col_energy[center_min:center_max]
    if center_energy.size == 0:
        return False

    divider_x = center_min + int(np.argmax(center_energy))
    peak_energy = float(col_energy[divider_x])
    baseline = float(np.median(col_energy)) + 1e-6

    if peak_energy < baseline * 2.25:
        return False

    col_slice = edge_strength[:, max(0, divider_x - 1):min(w, divider_x + 2)]
    row_strength = col_slice.mean(axis=1)
    row_baseline = float(np.median(row_strength)) + 1e-6
    strong_frac = float(np.mean(row_strength > row_baseline * 1.65))

    return strong_frac > 0.36


def image_is_overcropped_subject(data):
    """Reject isolated/cropped subjects on plain backgrounds. Keep this modest so the pool is not starved."""
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]

    if max(w, h) > 720:
        scale = 720.0 / max(w, h)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    blur = cv2.GaussianBlur(gray, (17, 17), 0)
    edges = cv2.Canny(blur, 38, 120)
    edge_density = float(np.mean(edges > 0))

    small = cv2.resize(img, (80, 80), interpolation=cv2.INTER_AREA)
    unique_colors = len(np.unique(small.reshape(-1, 3), axis=0))

    low_sat = hsv[:, :, 1] < 40
    bright = hsv[:, :, 2] > 150
    plain_bg_frac = float(np.mean(low_sat & bright))

    # Only strong plain-background cases. More subtle vertical cases are handled client-side by orientation.
    if plain_bg_frac > 0.48 and edge_density < 0.055:
        return True
    if unique_colors < 850 and edge_density < 0.042:
        return True

    return False


def render_html():
    sequence = build_slide_sequence(force_refresh=False)
    sequence_json = json.dumps(sequence)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>misshurry</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
html, body {{
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: #000;
  cursor: none;
  touch-action: none;
  overscroll-behavior: none;
  user-select: none;
  -webkit-user-select: none;
  -webkit-touch-callout: none;
}}
canvas {{
  display: block;
  width: 100vw;
  height: 100vh;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
}}
#debug-url {{
  position: fixed;
  bottom: 8px;
  left: 50%;
  transform: translateX(-50%);
  max-width: 92vw;
  padding: 4px 8px;
  border-radius: 999px;
  color: rgba(255,255,255,0.48);
  background: rgba(0,0,0,0.28);
  font: 10px monospace;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  z-index: 10;
  user-select: text;
  cursor: copy;
}}
#debug-url:hover {{
  color: rgba(255,255,255,0.92);
  background: rgba(0,0,0,0.62);
}}
</style>
</head>
<body>
<canvas id="view"></canvas>
<div id="debug-url" title="Click to copy image URL"></div>
<script>
let slides = {sequence_json};
const SEQUENCE_LENGTH_JS = {SEQUENCE_LENGTH};
const AUTO_ADVANCE_MS = {AUTO_ADVANCE_MS};
const IMAGE_REFRESH_MS = 60000;
const LOAD_TIMEOUT_MS = 6500;

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d", {{ willReadFrequently: true }});
const debugUrl = document.getElementById("debug-url");

let DPR = 1;
let mouseX = 0;
let mouseY = 0;
let shuffledPool = [];
let poolIndex = 0;
let failedSrcs = new Set();
let recentlyShown = [];
let currentPrepared = null;
let currentImage = null;
let currentSrc = null;
let loadingSlide = false;

function isTouchDevice() {{
  return window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
}}

function phoneIsPortrait() {{
  return isTouchDevice() && window.innerHeight > window.innerWidth;
}}

function deviceAllowsVerticalOnly() {{
  return phoneIsPortrait();
}}

function normalizeSlideList(newSlides) {{
  if (!Array.isArray(newSlides)) return [];
  return newSlides
    .filter(s => s && typeof s.src === "string" && s.src.length)
    .map(s => ({{ src: s.src, verticalOnly: Boolean(s.verticalOnly) }}));
}}

slides = normalizeSlideList(slides);

function syncContextQuality(targetCtx) {{
  targetCtx.imageSmoothingEnabled = true;
  targetCtx.imageSmoothingQuality = "medium";
}}

function resizeCanvas() {{
  DPR = isTouchDevice() ? 1 : Math.max(1, Math.min(window.devicePixelRatio || 1, 1.35));
  const w = window.innerWidth;
  const h = window.innerHeight;
  canvas.width = Math.round(w * DPR);
  canvas.height = Math.round(h * DPR);
  canvas.style.width = w + "px";
  canvas.style.height = h + "px";
  syncContextQuality(ctx);
  if (!mouseX && !mouseY) {{
    mouseX = canvas.width / 2;
    mouseY = canvas.height / 2;
  }}
}}

function fitCover(sw, sh, dw, dh) {{
  const scale = Math.max(dw / sw, dh / sh);
  const w = sw * scale;
  const h = sh * scale;
  return {{
    x: (dw - w) / 2,
    y: (dh - h) * 0.42,
    w,
    h
  }};
}}

function shuffleArray(arr) {{
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {{
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }}
  return a;
}}

function slideAllowedByOrientation(slide) {{
  return !slide.verticalOnly || deviceAllowsVerticalOnly();
}}

function imageLooksVerticalish(img) {{
  return img.height >= img.width * 0.92;
}}

function imageAllowedByOrientation(img, slide) {{
  if (deviceAllowsVerticalOnly()) return true;
  if (slide && slide.verticalOnly) return false;
  // Extra safety: computer/landscape skips vertical-ish images even if we have not tagged the URL yet.
  if (imageLooksVerticalish(img)) return false;
  return true;
}}

function refillPool() {{
  let candidates = slides
    .filter(s => slideAllowedByOrientation(s))
    .filter(s => !failedSrcs.has(s.src));

  if (currentSrc && candidates.length > 1) candidates = candidates.filter(s => s.src !== currentSrc);

  if (recentlyShown.length && candidates.length > recentlyShown.length + 8) {{
    const recent = new Set(recentlyShown);
    candidates = candidates.filter(s => !recent.has(s.src));
  }}

  shuffledPool = shuffleArray(candidates).slice(0, SEQUENCE_LENGTH_JS);
  poolIndex = 0;

  if (!shuffledPool.length && slides.length) {{
    failedSrcs.clear();
    recentlyShown = [];
    shuffledPool = shuffleArray(slides.filter(s => slideAllowedByOrientation(s))).slice(0, SEQUENCE_LENGTH_JS);
  }}
}}

function getNextSlide() {{
  if (!shuffledPool.length || poolIndex >= shuffledPool.length) refillPool();
  if (!shuffledPool.length) return null;
  const slide = shuffledPool[poolIndex];
  poolIndex += 1;
  return slide;
}}

function makeImage(sourceImage) {{
  const off = document.createElement("canvas");
  off.width = canvas.width;
  off.height = canvas.height;
  const offCtx = off.getContext("2d", {{ willReadFrequently: true }});
  syncContextQuality(offCtx);
  offCtx.fillStyle = "#000";
  offCtx.fillRect(0, 0, off.width, off.height);

  const fit = fitCover(sourceImage.width, sourceImage.height, off.width, off.height);
  offCtx.drawImage(sourceImage, 0, 0, sourceImage.width, sourceImage.height, fit.x, fit.y, fit.w, fit.h);

  const imageData = offCtx.getImageData(0, 0, off.width, off.height);
  const data = imageData.data;
  const levels = 22;
  const step = 255 / (levels - 1);

  for (let i = 0; i < data.length; i += 4) {{
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    let gray = 0.299 * r + 0.587 * g + 0.114 * b;
    gray = Math.round(gray / step) * step;
    data[i] = gray;
    data[i + 1] = gray;
    data[i + 2] = gray;
    data[i + 3] = 255;
  }}

  offCtx.putImageData(imageData, 0, 0);
  return off;
}}

function drawBlack() {{
  ctx.globalCompositeOperation = "source-over";
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}}

function drawFlashlight() {{
  if (!currentPrepared) {{
    drawBlack();
    return;
  }}

  const radiusMultiplier = isTouchDevice() ? 0.145 : 0.070;
  const radius = Math.sqrt(canvas.width * canvas.width + canvas.height * canvas.height) * radiusMultiplier;

  ctx.globalCompositeOperation = "source-over";
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  const cutout = ctx.createRadialGradient(mouseX, mouseY, 0, mouseX, mouseY, radius);
  cutout.addColorStop(0.00, "rgba(255,244,170,1.00)");
  cutout.addColorStop(0.20, "rgba(255,228,125,0.86)");
  cutout.addColorStop(0.50, "rgba(255,198,70,0.50)");
  cutout.addColorStop(0.82, "rgba(255,170,40,0.20)");
  cutout.addColorStop(1.00, "rgba(255,150,20,0.00)");

  ctx.globalCompositeOperation = "destination-out";
  ctx.fillStyle = cutout;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.globalCompositeOperation = "destination-over";
  ctx.drawImage(currentPrepared, 0, 0, canvas.width, canvas.height);

  ctx.globalCompositeOperation = "source-over";
  const warm = ctx.createRadialGradient(mouseX, mouseY, 0, mouseX, mouseY, radius * 1.12);
  warm.addColorStop(0.00, "rgba(255,210,75,0.42)");
  warm.addColorStop(0.45, "rgba(255,185,45,0.24)");
  warm.addColorStop(0.85, "rgba(255,155,20,0.10)");
  warm.addColorStop(1.00, "rgba(255,140,0,0.00)");
  ctx.fillStyle = warm;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}}

function updateDebugUrl(src) {{
  if (!debugUrl || !src) return;
  const rawUrl = decodeURIComponent(src.replace("/proxy?url=", ""));
  debugUrl.textContent = rawUrl;
  debugUrl.dataset.url = rawUrl;
  debugUrl.title = "Click to copy: " + rawUrl;
}}

function showImage(img, src) {{
  currentImage = img;
  currentSrc = src;
  currentPrepared = makeImage(img);
  recentlyShown.push(src);
  const maxRecent = Math.min(220, Math.max(35, Math.floor(slides.length * 0.65)));
  if (recentlyShown.length > maxRecent) recentlyShown.shift();
  updateDebugUrl(src);
  drawFlashlight();
}}

function loadRandomSlide(attempts = 0) {{
  if (loadingSlide && attempts === 0) return;

  if (!slides.length) {{
    if (!currentPrepared) drawBlack();
    return;
  }}

  if (attempts > 60) {{
    loadingSlide = false;
    failedSrcs.clear();
    recentlyShown = [];
    refillPool();
    return;
  }}

  loadingSlide = true;
  const slide = getNextSlide();
  if (!slide) {{
    loadingSlide = false;
    refillPool();
    return;
  }}

  const loader = new Image();
  loader.decoding = "async";

  let finished = false;
  const timeout = setTimeout(() => {{
    if (finished) return;
    finished = true;
    failedSrcs.add(slide.src);
    loadingSlide = false;
    loadRandomSlide(attempts + 1);
  }}, LOAD_TIMEOUT_MS);

  loader.onload = () => {{
    if (finished) return;
    finished = true;
    clearTimeout(timeout);

    if (!imageAllowedByOrientation(loader, slide)) {{
      failedSrcs.add(slide.src);
      loadingSlide = false;
      loadRandomSlide(attempts + 1);
      return;
    }}

    try {{
      showImage(loader, slide.src);
    }} catch (err) {{
      failedSrcs.add(slide.src);
      loadingSlide = false;
      loadRandomSlide(attempts + 1);
      return;
    }}

    loadingSlide = false;
  }};

  loader.onerror = () => {{
    if (finished) return;
    finished = true;
    clearTimeout(timeout);
    failedSrcs.add(slide.src);
    loadingSlide = false;
    loadRandomSlide(attempts + 1);
  }};

  loader.src = slide.src;
}}

async function refreshImagePool() {{
  try {{
    const response = await fetch("/images?ts=" + Date.now(), {{ cache: "no-store" }});
    if (!response.ok) return;
    const fresh = normalizeSlideList(await response.json());
    const existing = new Set(slides.map(s => s.src));
    const added = [];
    for (const slide of fresh) {{
      if (!existing.has(slide.src)) {{
        existing.add(slide.src);
        added.push(slide);
      }}
    }}
    if (!added.length) return;
    slides = shuffleArray(added).concat(slides);
    if (slides.length > SEQUENCE_LENGTH_JS * 2) slides = slides.slice(0, SEQUENCE_LENGTH_JS * 2);
    refillPool();
  }} catch (err) {{
    // quiet fail
  }}
}}

function moveFlashlightToClientPoint(clientX, clientY) {{
  const rect = canvas.getBoundingClientRect();
  const offsetY = isTouchDevice() ? window.innerHeight * 0.12 : 0;
  mouseX = (clientX - rect.left) * DPR;
  mouseY = ((clientY - rect.top) - offsetY) * DPR;
  drawFlashlight();
}}

canvas.addEventListener("mousemove", (e) => {{
  if (!isTouchDevice()) moveFlashlightToClientPoint(e.clientX, e.clientY);
}});
canvas.addEventListener("pointerdown", (e) => {{
  e.preventDefault();
  moveFlashlightToClientPoint(e.clientX, e.clientY);
}}, {{ passive: false }});
canvas.addEventListener("pointermove", (e) => {{
  e.preventDefault();
  moveFlashlightToClientPoint(e.clientX, e.clientY);
}}, {{ passive: false }});
canvas.addEventListener("pointerup", (e) => {{
  e.preventDefault();
}}, {{ passive: false }});
canvas.addEventListener("click", (e) => {{
  // Timer-only slideshow. Clicks do NOT change images.
  e.preventDefault();
}}, {{ passive: false }});

if (debugUrl) {{
  debugUrl.addEventListener("click", async (e) => {{
    e.stopPropagation();
    const url = debugUrl.dataset.url || debugUrl.textContent || "";
    if (!url) return;
    try {{
      await navigator.clipboard.writeText(url);
      const oldText = debugUrl.textContent;
      debugUrl.textContent = "copied: " + url;
      setTimeout(() => {{ debugUrl.textContent = oldText; }}, 900);
    }} catch (err) {{
      window.prompt("Copy this image URL:", url);
    }}
  }});
}}

window.addEventListener("resize", () => {{
  resizeCanvas();
  failedSrcs.clear();
  refillPool();
  if (currentImage && imageAllowedByOrientation(currentImage, {{ src: currentSrc, verticalOnly: false }})) {{
    currentPrepared = makeImage(currentImage);
    drawFlashlight();
  }} else {{
    loadRandomSlide();
  }}
}});

resizeCanvas();
mouseX = canvas.width / 2;
mouseY = canvas.height / 2;
refillPool();
loadRandomSlide();
setInterval(loadRandomSlide, AUTO_ADVANCE_MS);
setInterval(refreshImagePool, IMAGE_REFRESH_MS);
setInterval(() => {{ if (!currentPrepared && !loadingSlide) loadRandomSlide(); }}, 1500);
</script>
</body>
</html>
"""


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
            html_doc = render_html()
            data = html_doc.encode("utf-8")
            self.safe_send_bytes(200, data, "text/html; charset=utf-8")
            return

        if path in ["/images", "/images.json"]:
            sequence = build_slide_sequence(force_refresh=True)
            data = json.dumps(sequence).encode("utf-8")
            self.safe_send_bytes(
                200,
                data,
                "application/json; charset=utf-8",
                {"Cache-Control": "no-store"},
            )
            return

        if path == "/proxy":
            url = query.get("url", [""])[0]

            if not url:
                self.safe_send_bytes(400, b"Missing image URL")
                return

            if url_is_known_bad(url):
                REJECT_CACHE[url] = {"time": time.time()}
                print("[REJECT known bad]", url)
                self.safe_send_bytes(415, b"Known bad BBC graphic", extra_headers={"Cache-Control": "no-store"})
                return

            cleanup_proxy_cache()
            cached = PROXY_CACHE.get(url)
            if cached and time.time() - cached["time"] < PROXY_CACHE_SECONDS:
                self.safe_send_bytes(
                    200,
                    cached["data"],
                    cached["content_type"],
                    {"Cache-Control": "public, max-age=300"},
                )
                return

            try:
                data, content_type = fetch_bytes(url, timeout=8)

                if not content_type.startswith("image/"):
                    REJECT_CACHE[url] = {"time": time.time()}
                    self.safe_send_bytes(415, b"Not an image", extra_headers={"Cache-Control": "no-store"})
                    return

                test_data = data
                vertical_only = url_is_vertical_only(url)

                try:
                    arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                    if img is not None:
                        if image_is_probably_full_graphic_page(data):
                            REJECT_CACHE[url] = {"time": time.time()}
                            print("[REJECT graphic pre-crop]", url)
                            self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                            return

                        cropped, did_crop = crop_top_if_needed(img, url)
                        if cropped is not None and cropped.size > 0:
                            ok, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 96])
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

                if image_has_center_divider(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT divider]", url)
                    self.safe_send_bytes(415, b"Rejected center divider", extra_headers={"Cache-Control": "no-store"})
                    return

                if (not vertical_only) and image_is_overcropped_subject(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT overcropped]", url)
                    self.safe_send_bytes(415, b"Rejected overcropped subject", extra_headers={"Cache-Control": "no-store"})
                    return

                print("[SERVE]", url)
                PROXY_CACHE[url] = {
                    "time": time.time(),
                    "data": data,
                    "content_type": content_type,
                }

                self.safe_send_bytes(
                    200,
                    data,
                    content_type,
                    {"Cache-Control": "public, max-age=300"},
                )
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
    print("Higher-res BBC URLs: ON")
    print("Graphic rejection: ON")
    print("BBC logos: crop top")
    print("VOICE logo special crop: ON")
    print("Vertical-only URL fragments: ON")
    print("Timer-only slideshow: every 5 seconds")
    print("Click-to-change: OFF")
    print("Bottom URL copy link: ON")
    print(f"Serving at http://localhost:{PORT}")
    print()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
