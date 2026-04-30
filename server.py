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
    # Core BBC news feeds — broader pool so the scene filter still has lots to choose from.
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

    # Extra BBC feeds for more usable scene images.
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/health/rss.xml",
    "https://feeds.bbci.co.uk/news/in_pictures/rss.xml",
    "https://feeds.bbci.co.uk/sport/rss.xml",
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
    "https://feeds.bbci.co.uk/sport/formula1/rss.xml",
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

MAX_IMAGE_POOL = 850
SEQUENCE_LENGTH = 650

IMAGE_CACHE = {"time": 0, "images": []}
CACHE_SECONDS = 30

PROXY_CACHE = {}
PROXY_CACHE_SECONDS = 300
PROXY_CACHE_MAX_ITEMS = 300

REJECT_CACHE = {}
REJECT_CACHE_SECONDS = 1800

KNOWN_BAD_URL_FRAGMENTS = [
    "p0l7jnbt",
    "p0kxxp17",
    "p0n9y769",
    "3a08bc10",
    "c5a74450",
    "f53b6250",
    "p0ngd4cc",
    "166137e0",
    "acb55400",
]


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


def get_bbc_images(limit=MAX_IMAGE_POOL):
    now = time.time()

    if IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
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

            for item in items[:650]:
                if len(images) >= limit:
                    break

                img = extract_rss_item_image(item)
                if not img:
                    continue

                if any(bad in img for bad in KNOWN_BAD_URL_FRAGMENTS):
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


def crop_top_if_needed(img):
    if img is None or img.size == 0:
        return img, False

    try:
        if not top_has_bbc_branding(img):
            return img, False

        h, w = img.shape[:2]
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

    # Detect thin bright/white vertical graphic seams that run nearly top-to-bottom.
    # This catches BBC graphic cards with white dividing lines without rejecting most
    # normal photos that only have local edges, poles, door frames, etc.
    bright = gray > 218
    center_min = int(w * 0.18)
    center_max = int(w * 0.82)

    for x in range(center_min, center_max):
        band = bright[:, max(0, x - 1):min(w, x + 2)]
        bright_by_row = np.mean(band, axis=1) > 0.45
        full_height_frac = float(np.mean(bright_by_row))

        if full_height_frac > 0.58:
            return True

        # Also catch dashed-looking vertical white lines with small gaps.
        if full_height_frac > 0.42:
            transitions = np.diff(bright_by_row.astype(np.int8))
            segment_count = int(np.sum(transitions == 1))
            if segment_count <= 8:
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


def image_is_overcropped_subject(data):
    """Reject isolated, cutout-like subjects on plain or low-context backgrounds."""
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]

    if max(w, h) > 700:
        scale = 700.0 / max(w, h)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    blur = cv2.GaussianBlur(gray, (21, 21), 0)
    edges = cv2.Canny(blur, 40, 120)
    edge_density = float(np.mean(edges > 0))

    center = gray[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)]
    outer = gray.copy()
    outer[int(h * 0.2):int(h * 0.8), int(w * 0.2):int(w * 0.8)] = 0

    center_std = float(np.std(center)) if center.size else 0.0
    outer_std = float(np.std(outer)) + 1e-6

    small = cv2.resize(img, (80, 80), interpolation=cv2.INTER_AREA)
    unique_colors = len(np.unique(small.reshape(-1, 3), axis=0))

    low_sat = hsv[:, :, 1] < 38
    bright = hsv[:, :, 2] > 150
    plain_bg_frac = float(np.mean(low_sat & bright))

    if plain_bg_frac > 0.24 and edge_density < 0.09:
        return True

    if unique_colors < 1700 and edge_density < 0.085:
        return True

    if center_std > outer_std * 1.9 and edge_density < 0.11:
        return True

    return False

def image_is_close_subject_not_scene(data):
    """Reject images that read like a single close subject/portrait rather than a scene."""
    try:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception:
        return False

    if img is None or img.size == 0:
        return False

    h, w = img.shape[:2]

    if w < 160 or h < 120:
        return False

    if max(w, h) > 720:
        scale = 720.0 / max(w, h)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    ycrcb = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)

    edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 45, 135)
    edge = edges > 0

    center = edge[int(h * 0.14):int(h * 0.86), int(w * 0.18):int(w * 0.82)]
    outer = edge.copy()
    outer[int(h * 0.14):int(h * 0.86), int(w * 0.18):int(w * 0.82)] = False

    center_edge = float(np.mean(center)) if center.size else 0.0
    outer_edge = float(np.mean(outer)) + 1e-6
    total_edge = float(np.mean(edge))

    y = ycrcb[:, :, 0]
    cr = ycrcb[:, :, 1]
    cb = ycrcb[:, :, 2]
    skin = (y > 45) & (cr > 132) & (cr < 178) & (cb > 78) & (cb < 135)

    upper_center_skin = skin[int(h * 0.05):int(h * 0.72), int(w * 0.22):int(w * 0.78)]
    skin_frac_upper_center = float(np.mean(upper_center_skin)) if upper_center_skin.size else 0.0
    skin_frac_total = float(np.mean(skin))

    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    low_context = ((sat < 45) & (val > 105)) | (val < 38)
    low_context_frac = float(np.mean(low_context))

    if center_edge > outer_edge * 2.35 and total_edge < 0.105:
        return True

    if skin_frac_upper_center > 0.075 and center_edge > outer_edge * 1.55 and total_edge < 0.13:
        return True

    if skin_frac_total > 0.16 and outer_edge < 0.030 and total_edge < 0.12:
        return True

    if low_context_frac > 0.52 and center_edge > outer_edge * 1.75 and total_edge < 0.115:
        return True

    return False
def render_html():
    images = get_bbc_images(limit=MAX_IMAGE_POOL)
    random.shuffle(images)

    sequence = []
    for img in images:
        proxied = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({"src": proxied})

    sequence_json = json.dumps(sequence)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>misshurry</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
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

</style>
</head>
<body>
<canvas id="view"></canvas>

<script>
let slides = {sequence_json};
const SEQUENCE_LENGTH_JS = {SEQUENCE_LENGTH};

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d", {{ alpha: false }});

let currentPrepared = null;
let currentImage = null;
let currentSrc = null;
let nextPrepared = null;
let nextImage = null;
let nextSrc = null;
let preparingNext = false;
let changingImage = false;

let mouseX = 0;
let mouseY = 0;
let shuffledPool = [];
let poolIndex = 0;
let DPR = 1;
let VIEW_W = window.innerWidth;
let VIEW_H = window.innerHeight;
let lastCanvasW = 0;
let lastCanvasH = 0;

function isTouchDevice() {{
  return window.matchMedia("(pointer: coarse)").matches;
}}

function syncContextQuality(targetCtx) {{
  targetCtx.imageSmoothingEnabled = true;
  targetCtx.imageSmoothingQuality = "medium";
}}

function resizeCanvas(force = false) {{
  const newDPR = isTouchDevice() ? 1 : Math.max(1, Math.min(window.devicePixelRatio || 1, 1.25));
  const newW = window.innerWidth;
  const newH = window.innerHeight;
  const pixelW = Math.round(newW * newDPR);
  const pixelH = Math.round(newH * newDPR);

  if (!force && pixelW === lastCanvasW && pixelH === lastCanvasH) return false;

  DPR = newDPR;
  VIEW_W = newW;
  VIEW_H = newH;
  lastCanvasW = pixelW;
  lastCanvasH = pixelH;

  canvas.width = pixelW;
  canvas.height = pixelH;
  canvas.style.width = VIEW_W + "px";
  canvas.style.height = VIEW_H + "px";
  syncContextQuality(ctx);

  if (!mouseX && !mouseY) {{
    mouseX = canvas.width / 2;
    mouseY = canvas.height / 2;
  }}
  return true;
}}

function fitCover(sw, sh, dw, dh) {{
  const scale = Math.max(dw / sw, dh / sh);
  const w = sw * scale;
  const h = sh * scale;
  return {{ x: (dw - w) / 2, y: (dh - h) / 2, w, h }};
}}

function shuffleArray(arr) {{
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {{
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }}
  return a;
}}

function refillPool() {{
  let candidates = slides.map(s => s.src);
  if (currentSrc && candidates.length > 1) candidates = candidates.filter(src => src !== currentSrc);
  shuffledPool = shuffleArray(candidates).slice(0, SEQUENCE_LENGTH_JS);
  poolIndex = 0;
}}

function getNextRandomSrc() {{
  if (!shuffledPool.length || poolIndex >= shuffledPool.length) refillPool();
  if (!shuffledPool.length) return null;
  const src = shuffledPool[poolIndex];
  poolIndex += 1;
  return src;
}}

function makeImage(sourceImage) {{
  const off = document.createElement("canvas");
  off.width = canvas.width;
  off.height = canvas.height;
  const offCtx = off.getContext("2d", {{ alpha: false }});
  syncContextQuality(offCtx);

  offCtx.fillStyle = "#000";
  offCtx.fillRect(0, 0, off.width, off.height);

  const fit = fitCover(sourceImage.width, sourceImage.height, off.width, off.height);
  offCtx.drawImage(sourceImage, 0, 0, sourceImage.width, sourceImage.height, fit.x, fit.y, fit.w, fit.h);

  const imageData = offCtx.getImageData(0, 0, off.width, off.height);
  const data = imageData.data;
  const levels = 24;
  const step = 255 / (levels - 1);

  for (let i = 0; i < data.length; i += 4) {{
    const grayRaw = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
    const gray = Math.round(grayRaw / step) * step;
    data[i] = gray;
    data[i + 1] = gray;
    data[i + 2] = gray;
    data[i + 3] = 255;
  }}

  offCtx.putImageData(imageData, 0, 0);
  return off;
}}

function drawFallbackMessage() {{
  ctx.globalCompositeOperation = "source-over";
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}}

function drawFlashlight() {{
  if (!currentPrepared) {{
    drawFallbackMessage();
    return;
  }}

  const touch = isTouchDevice();
  const radius = Math.sqrt(canvas.width * canvas.width + canvas.height * canvas.height) * (touch ? 0.125 : 0.07);

  ctx.globalCompositeOperation = "source-over";
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

async function refreshImagePool() {{
  try {{
    const res = await fetch('/images.json?ts=' + Date.now(), {{ cache: 'no-store' }});
    if (!res.ok) return;
    const fresh = await res.json();
    const existing = new Set(slides.map(s => s.src));
    let added = 0;
    for (const item of fresh) {{
      if (item && item.src && !existing.has(item.src)) {{
        slides.push(item);
        existing.add(item.src);
        added += 1;
      }}
    }}
    if (slides.length > SEQUENCE_LENGTH_JS * 2) slides = shuffleArray(slides).slice(0, SEQUENCE_LENGTH_JS * 2);
    if (added > 0) refillPool();
  }} catch (e) {{}}
}}

function phoneIsPortrait() {{
  return window.innerHeight > window.innerWidth && window.innerWidth < 900;
}}

function imageIsPortraitish(img) {{
  return img.height >= img.width * 0.92;
}}

function shouldPreferPortrait(attempts) {{
  return phoneIsPortrait() && attempts < 6;
}}

function loadImageElement(src) {{
  return new Promise((resolve, reject) => {{
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = src;
  }});
}}

async function prepareNextSlide(attempts = 0) {{
  if (preparingNext || nextPrepared) return;
  if (!slides.length || attempts > 30) return;
  preparingNext = true;

  try {{
    const src = getNextRandomSrc();
    if (!src) return;
    const img = await loadImageElement(src);

    if (shouldPreferPortrait(attempts) && !imageIsPortraitish(img)) {{
      preparingNext = false;
      prepareNextSlide(attempts + 1);
      return;
    }}

    await new Promise(requestAnimationFrame);
    nextPrepared = makeImage(img);
    nextImage = img;
    nextSrc = src;
  }} catch (e) {{
    const badSrc = shuffledPool[Math.max(0, poolIndex - 1)];
    if (badSrc) slides = slides.filter(s => s.src !== badSrc);
    preparingNext = false;
    prepareNextSlide(attempts + 1);
    return;
  }} finally {{
    preparingNext = false;
  }}
}}

async function showPreparedOrPrepareNow() {{
  if (changingImage) return;
  changingImage = true;

  try {{
    if (!nextPrepared) await prepareNextSlide();

    if (nextPrepared) {{
      currentPrepared = nextPrepared;
      currentImage = nextImage;
      currentSrc = nextSrc;
      nextPrepared = null;
      nextImage = null;
      nextSrc = null;
      drawFlashlight();
      setTimeout(() => prepareNextSlide(), 50);
    }}
  }} finally {{
    changingImage = false;
  }}
}}

function moveFlashlightToClientPoint(clientX, clientY) {{
  const rect = canvas.getBoundingClientRect();
  const touchOffset = isTouchDevice() ? window.innerHeight * 0.12 : 0;
  mouseX = (clientX - rect.left) * DPR;
  mouseY = ((clientY - rect.top) - touchOffset) * DPR;
  drawFlashlight();
}}

const AUTO_ADVANCE_MS = 5000;

canvas.addEventListener("pointermove", (e) => {{
  if (e.pointerType === "mouse") moveFlashlightToClientPoint(e.clientX, e.clientY);
  e.preventDefault();
}}, {{ passive: false }});

canvas.addEventListener("pointerdown", (e) => {{
  canvas.setPointerCapture?.(e.pointerId);
  moveFlashlightToClientPoint(e.clientX, e.clientY);
  e.preventDefault();
}}, {{ passive: false }});

canvas.addEventListener("pointerup", (e) => {{
  canvas.releasePointerCapture?.(e.pointerId);
  e.preventDefault();
}}, {{ passive: false }});

canvas.addEventListener("pointercancel", (e) => e.preventDefault(), {{ passive: false }});
canvas.addEventListener("click", (e) => e.preventDefault());

setInterval(showPreparedOrPrepareNow, AUTO_ADVANCE_MS);
setInterval(refreshImagePool, 120000);

window.addEventListener("resize", () => {{
  if (resizeCanvas(true)) {{
    nextPrepared = null;
    if (currentImage) {{
      currentPrepared = makeImage(currentImage);
      drawFlashlight();
      setTimeout(() => prepareNextSlide(), 50);
    }}
  }}
}});

resizeCanvas(true);
mouseX = canvas.width / 2;
mouseY = canvas.height / 2;
refillPool();
showPreparedOrPrepareNow();
setTimeout(() => prepareNextSlide(), 500);
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

        if path == "/images.json":
            images = get_bbc_images(limit=MAX_IMAGE_POOL)
            random.shuffle(images)
            sequence = []
            for img in images:
                proxied = "/proxy?url=" + urllib.parse.quote(img, safe="")
                sequence.append({"src": proxied})

            data = json.dumps(sequence).encode("utf-8")
            self.safe_send_bytes(
                200,
                data,
                "application/json",
                {"Cache-Control": "no-store"},
            )
            return

        if path == "/proxy":
            url = query.get("url", [""])[0]

            if not url:
                self.safe_send_bytes(400, b"Missing image URL")
                return

            if any(bad in url for bad in KNOWN_BAD_URL_FRAGMENTS):
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

                try:
                    arr = np.frombuffer(data, np.uint8)
                    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)

                    if img is not None:
                        if image_is_probably_full_graphic_page(data):
                            REJECT_CACHE[url] = {"time": time.time()}
                            print("[REJECT graphic pre-crop]", url)
                            self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                            return

                        cropped, did_crop = crop_top_if_needed(img)

                        if cropped is not None and cropped.size > 0:
                            ok, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 98])

                            if ok:
                                data = encoded.tobytes()
                                test_data = data
                                content_type = "image/jpeg"

                                if did_crop:
                                    print("[CROP BBC top]", url)

                except Exception:
                    test_data = data

                if image_is_probably_full_graphic_page(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT graphic]", url)
                    self.safe_send_bytes(415, b"Rejected graphic page", extra_headers={"Cache-Control": "no-store"})
                    return

                if image_is_close_subject_not_scene(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT close subject]", url)
                    self.safe_send_bytes(415, b"Rejected close subject", extra_headers={"Cache-Control": "no-store"})
                    return

                if image_is_overcropped_subject(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT overcropped]", url)
                    self.safe_send_bytes(415, b"Rejected overcropped subject", extra_headers={"Cache-Control": "no-store"})
                    return

                if image_has_center_divider(test_data):
                    REJECT_CACHE[url] = {"time": time.time()}
                    print("[REJECT divider]", url)
                    self.safe_send_bytes(415, b"Rejected center divider", extra_headers={"Cache-Control": "no-store"})
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
    print("Posterization: slightly stronger")
    print("Light: phone larger, desktop smaller")
    print("Mobile portrait mode: prefer vertical images, then fallback")
    print("Auto-refresh image pool: ON")
    print("Auto-advance: every 5 seconds")
    print("Feeds: expanded BBC pool")
    print("Close-subject / generic portrait rejection: ON")
    print(f"Serving at http://localhost:{PORT}")
    print()

    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
