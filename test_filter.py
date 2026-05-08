"""
Regression checks for the scene-focused flashlight news-image server.
Run with:
    python3 test.py
"""

import cv2
import numpy as np

from server import (
    DIRECT_IMAGE_PAGES,
    KNOWN_BAD_URL_FRAGMENTS,
    RSS_FEEDS,
    SOURCE_PAGES,
    VERTICAL_ONLY_URL_FRAGMENTS,
    canonical_image_key,
    clean_extracted_image_url,
    image_bytes_are_vertical,
    image_url_looks_vertical_or_phone_crop,
    url_is_disallowed_graphic_asset,
    url_is_known_bad,
    url_is_vertical_only,
)

URL_TESTS = [
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/5a8f0590.jpg",
        True,
        False,
        False,
        "generic editorial image should be rejected",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/166137e0.jpg",
        False,
        True,
        True,
        "known vertical BBC image should be treated as vertical",
    ),
    (
        "https://ichef.bbci.co.uk/images/ic/1024x576/p0ngd4cc.jpg",
        True,
        False,
        False,
        "known bad cropped image should be rejected",
    ),
    (
        "https://assets.apnews.com/83/1f/238ba42a44b79f31af552a46e097/typeshift.svg",
        True,
        False,
        False,
        "AP svg graphic should be hard rejected",
    ),
    (
        "https://assets.apnews.com/fe/2c/0f8de78b47b890b0319ab14d9c4e/pileup.svg",
        True,
        False,
        False,
        "AP pileup svg should be hard rejected",
    ),
    (
        "https://assets.apnews.com/04/b6/ed98f9004995bc7af2a363e88ada/memoku.svg",
        True,
        False,
        False,
        "AP memoku svg should be hard rejected",
    ),
    (
        "https://dims.apnews.com/dims4/default/abc/2147483647/strip/true/crop/1200x1800+0+0/resize/800x1200!/format/webp/quality/90/?url=https%3A%2F%2Fassets.apnews.com%2Faa%2Fbb%2Fportrait.jpg",
        False,
        False,
        True,
        "AP portrait crop/resize URL should be rejected as vertical",
    ),
    (
        "https://dims.apnews.com/dims4/default/abc/2147483647/strip/true/crop/1800x1200+0+0/resize/1200x800!/format/webp/quality/90/?url=https%3A%2F%2Fassets.apnews.com%2Faa%2Fbb%2Flandscape.jpg",
        False,
        False,
        False,
        "AP landscape crop/resize URL should pass vertical URL check",
    ),
]


def encoded_jpeg(width, height):
    img = np.zeros((height, width, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode(".jpg", img)
    if not ok:
        raise RuntimeError("could not encode test image")
    return encoded.tobytes()


def main():
    failures = 0

    for url, expected_bad, expected_vertical_only, expected_verticalish, label in URL_TESTS:
        got_bad = url_is_known_bad(url) or url_is_disallowed_graphic_asset(url)
        got_vertical_only = url_is_vertical_only(url)
        got_verticalish = image_url_looks_vertical_or_phone_crop(url)
        ok = (
            got_bad == expected_bad
            and got_vertical_only == expected_vertical_only
            and got_verticalish == expected_verticalish
        )
        if ok:
            print("PASS ✓", label)
        else:
            print("FAIL ✗", label)
            print(" ", url)
            print("   expected bad:", expected_bad, "got:", got_bad)
            print("   expected vertical_only:", expected_vertical_only, "got:", got_vertical_only)
            print("   expected verticalish:", expected_verticalish, "got:", got_verticalish)
            failures += 1

    overlap = set(KNOWN_BAD_URL_FRAGMENTS) & set(VERTICAL_ONLY_URL_FRAGMENTS)
    if overlap:
        print("FAIL ✗ overlap between hard-bad and vertical-only fragments")
        print(sorted(overlap))
        failures += 1
    else:
        print("PASS ✓ no overlap between hard-bad and vertical-only fragments")

    ap_a = (
        "https://dims.apnews.com/dims4/default/abc/2147483647/"
        "strip/true/resize/1200x800!/format/webp/quality/90/"
        "?url=https://assets.apnews.com/37/ec/example.jpg"
    )
    ap_b = "https://assets.apnews.com/37/ec/example.jpg"
    if canonical_image_key(ap_a) == canonical_image_key(ap_b):
        print("PASS ✓ AP resized URLs dedupe to the underlying asset")
    else:
        print("FAIL ✗ AP dims URL did not dedupe to underlying asset")
        failures += 1

    if clean_extracted_image_url("https://assets.apnews.com/83/1f/typeshift.svg") is None:
        print("PASS ✓ clean_extracted_image_url rejects SVG")
    else:
        print("FAIL ✗ clean_extracted_image_url allowed SVG")
        failures += 1

    if image_bytes_are_vertical(encoded_jpeg(500, 900)):
        print("PASS ✓ proxy-level byte check rejects true vertical image")
    else:
        print("FAIL ✗ proxy-level byte check missed vertical image")
        failures += 1

    if not image_bytes_are_vertical(encoded_jpeg(900, 500)):
        print("PASS ✓ proxy-level byte check allows landscape image")
    else:
        print("FAIL ✗ proxy-level byte check rejected landscape image")
        failures += 1

    forbidden_source_terms = ["technology", "entertainment", "business", "sports", "science_and_environment", "health", "religion"]
    all_sources = RSS_FEEDS + SOURCE_PAGES + DIRECT_IMAGE_PAGES
    bad_sources = [u for u in all_sources if any(term in u for term in forbidden_source_terms)]
    if bad_sources:
        print("FAIL ✗ generic/portrait-heavy sources still present")
        for src in bad_sources:
            print(" ", src)
        failures += 1
    else:
        print("PASS ✓ generic/portrait-heavy source pages are excluded")

    required_terms = ["africa", "asia", "europe", "latin", "middle-east", "reuters.com/world"]
    source_text = "\n".join(all_sources)
    missing = [term for term in required_terms if term not in source_text]
    if missing:
        print("FAIL ✗ broad geographic source coverage missing:", missing)
        failures += 1
    else:
        print("PASS ✓ broad geographic source coverage is present")

    expected = len(URL_TESTS) + 7
    print()
    print(f"{expected - failures} passed, {failures} failed")
    print()
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
