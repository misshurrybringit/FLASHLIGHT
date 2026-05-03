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

MAX_IMAGE_POOL = 900
SEQUENCE_LENGTH = 700
RSS_ITEMS_PER_FEED = 800

IMAGE_CACHE = {"time": 0, "images": []}
CACHE_SECONDS = 45

PROXY_CACHE = {}
PROXY_CACHE_SECONDS = 300
PROXY_CACHE_MAX_ITEMS = 320

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
]

# These are allowed ONLY when the device is vertical/portrait.
# On desktop or horizontal screens, JavaScript skips them.
VERTICAL_ONLY_URL_FRAGMENTS = [
    "166137e0",
    "3600d2f0",
]

# These get a small top crop for a BBC VOICE-style logo instead of rejection.
VOICE_LOGO_CROP_FRAGMENTS = [
    "f16b6b80",
]


def url_has_fragment(url, fragments):
    return any(fragment in url for fragment in fragments)


def url_is_vertical_only(url):
    return url_has_fragment(url, VERTICAL_ONLY_URL_FRAGMENTS)


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
    for key in list(cache.keys()):
        if now - cache[key]["time"] > max_age:
            cache.pop(key, None)


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
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description.text, re.IGNORECASE)
        if match:
            return upgrade_bbc_image_url(match.group(1))
    return None


def get_bbc_images(limit=MAX_IMAGE_POOL, force_refresh=False):
    now = time.time()
    if not force_refresh and IMAGE_CACHE["images"] and now - IMAGE_CACHE["time"] < CACHE_SECONDS:
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
            for item in items[:RSS_ITEMS_PER_FEED]:
                if len(images) >= limit:
                    break
                img = extract_rss_item_image(item)
                if not img:
                    continue
                if url_has_fragment(img, KNOWN_BAD_URL_FRAGMENTS):
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
    sequence = []
    for img in get_bbc_images(limit=MAX_IMAGE_POOL, force_refresh=force_refresh):
        proxied = "/proxy?url=" + urllib.parse.quote(img, safe="")
        sequence.append({"src": proxied, "verticalOnly": url_is_vertical_only(img)})
    random.shuffle(sequence)
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
        if url_has_fragment(url, VOICE_LOGO_CROP_FRAGMENTS):
            cropped = img[int(h * 0.11):, :]
            if cropped is not None and cropped.size > 0:
                return cropped, True

        if not top_has_bbc_branding(img):
            return img, False
        cropped = img[int(h * 0.24):, :]
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
    bright = gray > 218
    center_min = int(w * 0.18)
    center_max = int(w * 0.82)
    for x in range(center_min, center_max):
        band = bright[:, max(0, x - 1):min(w, x + 2)]
        bright_by_row = np.mean(band, axis=1) > 0.45
        full_height_frac = float(np.mean(bright_by_row))
        if full_height_frac > 0.58:
            return True
        if full_height_frac > 0.42:
            transitions = np.diff(bright_by_row.astype(np.int8))
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


def image_is_overcropped_subject(data):
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
    sequence_json = json.dumps(build_slide_sequence(force_refresh=False))
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>misshurry</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
html, body {
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
}
canvas {
  display: block;
  width: 100vw;
  height: 100vh;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
}
</style>
</head>
<body>
<canvas id="view"></canvas>
<script>
let slides = __SEQUENCE_JSON__;
const SEQUENCE_LENGTH_JS = __SEQUENCE_LENGTH__;
const AUTO_ADVANCE_MS = 5000;
const IMAGE_REFRESH_MS = 60000;
const LOAD_TIMEOUT_MS = 6500;

const canvas = document.getElementById("view");
const ctx = canvas.getContext("2d", { alpha: false, willReadFrequently: false });

let DPR = 1;
let mouseX = 0;
let mouseY = 0;
let currentPrepared = null;
let currentSrc = null;
let loadingSlide = false;
let shuffledPool = [];
let poolIndex = 0;
let failedSrcs = new Set();
let recentlyShown = [];

function isTouchDevice() {
  return window.matchMedia && window.matchMedia("(pointer: coarse)").matches;
}

function deviceIsVertical() {
  return window.innerHeight > window.innerWidth;
}

function slideAllowedForDevice(slide) {
  return !(slide.verticalOnly && !deviceIsVertical());
}

function syncContextQuality(targetCtx) {
  targetCtx.imageSmoothingEnabled = true;
  targetCtx.imageSmoothingQuality = "high";
}

function resizeCanvas() {
  DPR = Math.max(1, Math.min(window.devicePixelRatio || 1, isTouchDevice() ? 1.2 : 1.45));
  canvas.width = Math.round(window.innerWidth * DPR);
  canvas.height = Math.round(window.innerHeight * DPR);
  canvas.style.width = window.innerWidth + "px";
  canvas.style.height = window.innerHeight + "px";
  syncContextQuality(ctx);
  if (!mouseX && !mouseY) {
    mouseX = canvas.width / 2;
    mouseY = canvas.height / 2;
  }
}

function fitCover(sw, sh, dw, dh) {
  const scale = Math.max(dw / sw, dh / sh);
  const w = sw * scale;
  const h = sh * scale;
  return { x: (dw - w) / 2, y: (dh - h) * 0.42, w, h };
}

function shuffleArray(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function normalizeSlideList(newSlides) {
  if (!Array.isArray(newSlides)) return [];
  return newSlides
    .filter(s => s && typeof s.src === "string" && s.src.length)
    .map(s => ({ src: s.src, verticalOnly: !!s.verticalOnly }));
}

function mergeFreshSlides(newSlides) {
  const incoming = normalizeSlideList(newSlides);
  if (!incoming.length) return;
  const existing = new Set(slides.map(s => s.src));
  const added = [];
  for (const slide of incoming) {
    if (!existing.has(slide.src)) {
      existing.add(slide.src);
      added.push(slide);
    }
  }
  if (!added.length) return;
  slides = shuffleArray(added).concat(slides);
  if (slides.length > SEQUENCE_LENGTH_JS * 2) slides = slides.slice(0, SEQUENCE_LENGTH_JS * 2);
  refillPool();
}

async function checkForFreshImages() {
  try {
    const response = await fetch("/images?ts=" + Date.now(), { cache: "no-store" });
    if (!response.ok) return;
    mergeFreshSlides(await response.json());
  } catch (err) {}
}

function refillPool() {
  let candidates = slides
    .filter(slideAllowedForDevice)
    .map(s => s.src)
    .filter(src => !failedSrcs.has(src));

  if (currentSrc && candidates.length > 1) candidates = candidates.filter(src => src !== currentSrc);
  if (recentlyShown.length && candidates.length > recentlyShown.length + 8) {
    const recent = new Set(recentlyShown);
    candidates = candidates.filter(src => !recent.has(src));
  }
  shuffledPool = shuffleArray(candidates).slice(0, SEQUENCE_LENGTH_JS);
  poolIndex = 0;

  if (!shuffledPool.length) {
    failedSrcs.clear();
    recentlyShown = [];
    candidates = slides.filter(slideAllowedForDevice).map(s => s.src);
    shuffledPool = shuffleArray(candidates).slice(0, SEQUENCE_LENGTH_JS);
  }
}

function getNextRandomSrc() {
  if (!shuffledPool.length || poolIndex >= shuffledPool.length) refillPool();
  if (!shuffledPool.length) return null;
  const src = shuffledPool[poolIndex];
  poolIndex += 1;
  return src;
}

function addStrictMode(src, strictScene) {
  return src + (src.includes("?") ? "&" : "?") + "strict=" + (strictScene ? "1" : "0");
}

function makeImage(sourceImage) {
  const off = document.createElement("canvas");
  off.width = canvas.width;
  off.height = canvas.height;
  const offCtx = off.getContext("2d", { willReadFrequently: true });
  syncContextQuality(offCtx);
  offCtx.fillStyle = "#000";
  offCtx.fillRect(0, 0, off.width, off.height);
  const fit = fitCover(sourceImage.width, sourceImage.height, off.width, off.height);
  offCtx.drawImage(sourceImage, 0, 0, sourceImage.width, sourceImage.height, fit.x, fit.y, fit.w, fit.h);

  const imageData = offCtx.getImageData(0, 0, off.width, off.height);
  const data = imageData.data;
  const levels = 22;
  const step = 255 / (levels - 1);
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    let gray = 0.299 * r + 0.587 * g + 0.114 * b;
    gray = Math.round(gray / step) * step;
    data[i] = gray;
    data[i + 1] = gray;
    data[i + 2] = gray;
    data[i + 3] = 255;
  }
  offCtx.putImageData(imageData, 0, 0);
  return off;
}

function drawBlack() {
  ctx.globalCompositeOperation = "source-over";
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}

function drawFlashlight() {
  if (!currentPrepared) {
    drawBlack();
    return;
  }
  const radiusMultiplier = isTouchDevice() ? 0.155 : 0.070;
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
}

function showImage(img, src) {
  currentPrepared = makeImage(img);
  currentSrc = src;
  recentlyShown.push(src);
  const maxRecent = Math.min(220, Math.max(35, Math.floor(slides.length * 0.6)));
  if (recentlyShown.length > maxRecent) recentlyShown.shift();
  drawFlashlight();
}

function loadRandomSlide(attempts = 0) {
  if (loadingSlide && attempts === 0) return;
  if (!slides.length) return;
  if (attempts > 60) {
    loadingSlide = false;
    failedSrcs.clear();
    recentlyShown = [];
    refillPool();
    return;
  }

  loadingSlide = true;
  const src = getNextRandomSrc();
  if (!src) {
    loadingSlide = false;
    refillPool();
    return;
  }

  const strictScene = attempts < 18;
  const loader = new Image();
  loader.decoding = "async";
  let done = false;

  const timeout = setTimeout(() => {
    if (done) return;
    done = true;
    failedSrcs.add(src);
    loadingSlide = false;
    loadRandomSlide(attempts + 1);
  }, LOAD_TIMEOUT_MS);

  loader.onload = () => {
    if (done) return;
    done = true;
    clearTimeout(timeout);
    try {
      showImage(loader, src);
      loadingSlide = false;
    } catch (err) {
      failedSrcs.add(src);
      loadingSlide = false;
      loadRandomSlide(attempts + 1);
    }
  };

  loader.onerror = () => {
    if (done) return;
    done = true;
    clearTimeout(timeout);
    if (strictScene) {
      const fallback = new Image();
      fallback.decoding = "async";
      fallback.onload = () => {
        try {
          showImage(fallback, src);
        } catch (err) {
          failedSrcs.add(src);
        }
        loadingSlide = false;
      };
      fallback.onerror = () => {
        failedSrcs.add(src);
        loadingSlide = false;
        loadRandomSlide(attempts + 1);
      };
      fallback.src = addStrictMode(src, false);
      return;
    }
    failedSrcs.add(src);
    loadingSlide = false;
    loadRandomSlide(attempts + 1);
  };

  loader.src = addStrictMode(src, strictScene);
}

function updatePointerFromEvent(e) {
  const rect = canvas.getBoundingClientRect();
  const touchOffset = isTouchDevice() ? window.innerHeight * 0.12 : 0;
  mouseX = (e.clientX - rect.left) * DPR;
  mouseY = ((e.clientY - rect.top) - touchOffset) * DPR;
  drawFlashlight();
}

canvas.addEventListener("mousemove", (e) => {
  if (!isTouchDevice()) updatePointerFromEvent(e);
}, { passive: false });

canvas.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  if (canvas.setPointerCapture) {
    try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
  }
  updatePointerFromEvent(e);
}, { passive: false });

canvas.addEventListener("pointermove", (e) => {
  e.preventDefault();
  updatePointerFromEvent(e);
}, { passive: false });

canvas.addEventListener("touchstart", (e) => {
  e.preventDefault();
  if (e.touches && e.touches[0]) updatePointerFromEvent(e.touches[0]);
}, { passive: false });

canvas.addEventListener("touchmove", (e) => {
  e.preventDefault();
  if (e.touches && e.touches[0]) updatePointerFromEvent(e.touches[0]);
}, { passive: false });

canvas.addEventListener("click", (e) => {
  e.preventDefault();
}, { passive: false });

window.addEventListener("resize", () => {
  resizeCanvas();
  refillPool();
  if (currentSrc) {
    const img = new Image();
    img.onload = () => showImage(img, currentSrc);
    img.onerror = () => drawFlashlight();
    img.src = addStrictMode(currentSrc, false);
  } else {
    drawFlashlight();
  }
});

resizeCanvas();
mouseX = canvas.width / 2;
mouseY = canvas.height / 2;
refillPool();
loadRandomSlide();
setInterval(loadRandomSlide, AUTO_ADVANCE_MS);
setInterval(checkForFreshImages, IMAGE_REFRESH_MS);
setInterval(() => { if (!currentPrepared && !loadingSlide) loadRandomSlide(); }, 1200);
</script>
</body>
</html>
""".replace("__SEQUENCE_JSON__", sequence_json).replace("__SEQUENCE_LENGTH__", str(SEQUENCE_LENGTH))


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
            data = render_html().encode("utf-8")
            self.safe_send_bytes(200, data, "text/html; charset=utf-8")
            return

        if path == "/images":
            data = json.dumps(build_slide_sequence(force_refresh=True)).encode("utf-8")
            self.safe_send_bytes(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
            return

        if path == "/proxy":
            url = query.get("url", [""])[0]
            strict_scene = query.get("strict", ["1"])[0] != "0"
            if not url:
                self.safe_send_bytes(400, b"Missing image URL")
                return

            if url_has_fragment(url, KNOWN_BAD_URL_FRAGMENTS):
                REJECT_CACHE[url] = {"time": time.time()}
                print("[REJECT known bad]", url)
                self.safe_send_bytes(415, b"Known bad BBC graphic", extra_headers={"Cache-Control": "no-store"})
                return

            cleanup_proxy_cache()
            cache_key = url + "|strict=" + ("1" if strict_scene else "0")
            cached = PROXY_CACHE.get(cache_key)
            if cached and time.time() - cached["time"] < PROXY_CACHE_SECONDS:
                self.safe_send_bytes(200, cached["data"], cached["content_type"], {"Cache-Control": "public, max-age=300"})
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
                        cropped, did_crop = crop_top_if_needed(img, url)
                        if cropped is not None and cropped.size > 0:
                            ok, encoded = cv2.imencode(".jpg", cropped, [int(cv2.IMWRITE_JPEG_QUALITY), 96])
                            if ok:
                                data = encoded.tobytes()
                                test_data = data
                                content_type = "image/jpeg"
                                if did_crop:
                                    print("[CROP top/logo]", url)
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

                # Scene rules are applied only in strict mode. Vertical-only URLs are allowed
                # through the proxy, but the browser skips them on desktop/horizontal screens.
                if strict_scene and not url_is_vertical_only(url):
                    if image_is_close_subject_not_scene(test_data):
                        print("[REJECT close subject strict]", url)
                        self.safe_send_bytes(415, b"Rejected close subject", extra_headers={"Cache-Control": "no-store"})
                        return
                    if image_is_overcropped_subject(test_data):
                        print("[REJECT overcropped strict]", url)
                        self.safe_send_bytes(415, b"Rejected overcropped subject", extra_headers={"Cache-Control": "no-store"})
                        return

                print("[SERVE]", url)
                PROXY_CACHE[cache_key] = {"time": time.time(), "data": data, "content_type": content_type}
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
    print("Serving at http://localhost:%s" % PORT)
    print("Auto-advance: every 5 seconds")
    print("Click-to-change: OFF")
    print("Touch flashlight movement: ON")
    print("Vertical-only images: phone portrait only")
    print()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
