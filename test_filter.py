"""
Regression checks for the flashlight server URL/source rules.
Run with:
    python3 test.py

This does not download images. It checks URL rules, AP dedupe, scene-first
feed/source configuration, and AP-heavy final ordering logic.
"""

from server import (
    KNOWN_BAD_URL_FRAGMENTS,
    VERTICAL_ONLY_URL_FRAGMENTS,
    RSS_FEEDS,
    DIRECT_IMAGE_PAGES,
    SOURCE_PAGES,
    url_is_known_bad,
    url_is_vertical_only,
    url_needs_voice_crop,
    url_is_disallowed_graphic_asset,
    canonical_image_key,
    source_category,
    weighted_image_mix,
)

TESTS = [
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/dce9/live/166137e0-3f11-11f1-bd52-e755d604ece4.jpg",
        False, True, False,
        "vertical/cropped image should be vertical-phone only, not hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/627b/live/3600d2f0-2214-11f1-b297-95b0a0a8331e.jpg",
        False, True, False,
        "cropped full-body/vertical image should be vertical-phone only, not hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/1e87/live/9a3df7e0-4562-11f1-b55d-0f258dce1735.jpg",
        False, True, False,
        "vertical/cropped editorial image should be vertical-phone only",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/79d6/live/7b23ccb0-328c-11f1-b297-95b0a0a8331e.jpg",
        False, True, False,
        "vertical composition inside landscape frame should be vertical-phone only",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/6245/live/c022fa90-44a0-11f1-ac78-2112837ce2aa.jpg",
        False, True, False,
        "vertical-inside-landscape frame should be vertical-phone only",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/3ead/live/679152b0-3f40-11f1-ac78-2112837ce2aa.jpg",
        False, True, False,
        "vertical-inside-landscape frame should be vertical-phone only",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/d379/live/9f35a8f0-4545-11f1-8ea3-630273c214ab.jpg",
        True, False, False,
        "promotional / graphic editorial image should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/79f2/live/4c3e0ce0-3a47-11f1-8606-05fe34b06e1b.jpg",
        True, False, False,
        "graphic + cropped editorial image should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/4448/live/f16b6b80-43d5-11f1-bf3e-3d07e81b01ce.jpg",
        False, False, True,
        "VOICE-logo image should be cropped, not rejected",
    ),
    (
        "https://ichef.bbci.co.uk/images/ic/1024x576/p0ngd4cc.jpg",
        True, False, False,
        "known bad cropped/isolated image should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/6a8f/live/00a03cc0-3d2d-11f1-9d5c-8ba507d7dbde.jpg",
        True, False, False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/639a/live/929fd780-43d5-11f1-bf3e-3d07e81b01ce.jpg",
        True, False, False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/68be/live/06449360-4525-11f1-bd52-e755d604ece4.jpg",
        True, False, False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/bfc0/live/5a8f0590-3e59-11f1-8887-e93160959470.jpg",
        True, False, False,
        "generic/non-scene image should be hard blocked",
    ),
]


def check(label, condition):
    if condition:
        print(f"  PASS ✓ {label}")
        return 0
    print(f"  FAIL ✗ {label}")
    return 1


def main():
    failures = 0
    print()
    for url, expected_bad, expected_vertical, expected_voice_crop, label in TESTS:
        got_bad = url_is_known_bad(url)
        got_vertical = url_is_vertical_only(url)
        got_voice_crop = url_needs_voice_crop(url)

        ok = (
            got_bad == expected_bad
            and got_vertical == expected_vertical
            and got_voice_crop == expected_voice_crop
        )

        status = "PASS ✓" if ok else "FAIL ✗"
        print(f"  {status} {label}")
        if not ok:
            print(f"        url: {url}")
            print(f"        known_bad: expected {expected_bad}, got {got_bad}")
            print(f"        vertical_only: expected {expected_vertical}, got {got_vertical}")
            print(f"        voice_crop: expected {expected_voice_crop}, got {got_voice_crop}")
            failures += 1

    overlap = set(KNOWN_BAD_URL_FRAGMENTS) & set(VERTICAL_ONLY_URL_FRAGMENTS)
    failures += check("no overlap between hard-bad and vertical-only fragments", not overlap)
    if overlap:
        print(f"        overlap: {sorted(overlap)}")

    ap_a = "https://dims.apnews.com/dims4/default/abc/2147483647/strip/true/resize/1200x800!/format/webp/quality/90/?url=https://assets.apnews.com/37/ec/example.jpg"
    ap_b = "https://assets.apnews.com/37/ec/example.jpg"
    failures += check("AP dims URLs dedupe to the underlying asset", canonical_image_key(ap_a) == canonical_image_key(ap_b))

    generic_feed_terms = ["technology", "business", "health", "entertainment", "science"]
    failures += check(
        "generic-heavy BBC feeds are removed",
        not any(any(term in feed for term in generic_feed_terms) for feed in RSS_FEEDS if "bbc" in feed),
    )

    ap_direct_count = sum(1 for page in DIRECT_IMAGE_PAGES if "apnews.com" in page)
    failures += check("AP has a large direct-page crawl list", ap_direct_count >= 18)
    failures += check("AP direct pages are prioritized before non-AP pages", all("apnews.com" in p for p in DIRECT_IMAGE_PAGES[:10]))
    failures += check(
        "generic AP/Reuters business/tech/entertainment pages are not in direct/source pages",
        not any(any(term in page for term in ["business", "technology", "entertainment"]) for page in DIRECT_IMAGE_PAGES + SOURCE_PAGES),
    )


    graphic_assets = [
        "https://assets.apnews.com/83/1f/238ba42a44b79f31af552a46e097/typeshift.svg",
        "https://assets.apnews.com/fe/2c/0f8de78b47b890b0319ab14d9c4e/pileup.svg",
        "https://assets.apnews.com/04/b6/ed98f9004995bc7af2a363e88ada/memoku.svg",
    ]
    failures += check("AP SVG graphics are disallowed", all(url_is_disallowed_graphic_asset(u) for u in graphic_assets))

    failures += check("source_category detects AP dims URLs", source_category(ap_a) == "ap")
    failures += check("source_category detects BBC URLs", source_category("https://ichef.bbci.co.uk/example.jpg") == "bbc")

    sample = [
        f"https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/{i}/live/bbc-{i}.jpg" for i in range(600)
    ] + [
        f"https://assets.apnews.com/{i:02x}/scene-{i}.jpg" for i in range(80)
    ] + [
        f"https://static.reuters.com/example/scene-{i}.jpg" for i in range(30)
    ]
    mixed = weighted_image_mix(sample, limit=120)
    ap_in_first_120 = sum(1 for url in mixed if source_category(url) == "ap")
    bbc_in_first_120 = sum(1 for url in mixed if source_category(url) == "bbc")
    failures += check("weighted mix gives AP strong representation when AP exists", ap_in_first_120 >= 70)
    failures += check("weighted mix prevents BBC from flooding the first pool", bbc_in_first_120 <= 40)

    total = len(TESTS) + 11
    print()
    print(f"  {total - failures} passed, {failures} failed")
    print()
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
