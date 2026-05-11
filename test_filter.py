from server import (
    KNOWN_BAD_URL_FRAGMENTS,
    VERTICAL_ONLY_URL_FRAGMENTS,
    RSS_FEEDS,
    DIRECT_IMAGE_PAGES,
    url_is_known_bad,
    url_is_vertical_only,
    url_needs_voice_crop,
    clean_extracted_image_url,
    canonical_image_key,
)

TESTS = [
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/5a8f0590.jpg",
        True, False, False,
        "generic editorial image should be rejected",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/166137e0.jpg",
        False, True, False,
        "vertical-only image should pass vertical rule",
    ),
    (
        "https://ichef.bbci.co.uk/images/ic/1024x576/p0ngd4cc.jpg",
        True, False, False,
        "known bad cropped image should be rejected",
    ),
    (
        "https://assets.apnews.com/83/1f/238ba42a44b79f31af552a46e097/typeshift.svg",
        True, False, False,
        "AP svg graphic should be rejected",
    ),
    (
        "https://assets.apnews.com/fe/2c/0f8de78b47b890b0319ab14d9c4e/pileup.svg",
        True, False, False,
        "AP pileup svg should be rejected",
    ),
    (
        "https://dims.apnews.com/dims4/default/bf3595b/2147483647/strip/true/crop/1500x999+0+0/resize/944x629!/quality/90/?url=https%3A%2F%2Fassets.apnews.com%2Fb9%2F2f%2Fbae9d7794692aee65c34b848aae2%2Fstrait-of-hormuz-3x2-v2.jpg",
        True, False, False,
        "AP Strait of Hormuz map graphic should be rejected",
    ),
]


def main():
    failures = 0

    for url, expected_bad, expected_vertical, expected_voice, label in TESTS:
        got_bad = url_is_known_bad(url)
        got_vertical = url_is_vertical_only(url)
        got_voice = url_needs_voice_crop(url)
        ok = (
            got_bad == expected_bad
            and got_vertical == expected_vertical
            and got_voice == expected_voice
        )
        if ok:
            print("PASS ✓", label)
        else:
            print("FAIL ✗", label)
            print(" ", url)
            print("   expected bad:", expected_bad, "got:", got_bad)
            print("   expected vertical:", expected_vertical, "got:", got_vertical)
            print("   expected voice:", expected_voice, "got:", got_voice)
            failures += 1

    overlap = set(KNOWN_BAD_URL_FRAGMENTS) & set(VERTICAL_ONLY_URL_FRAGMENTS)
    if overlap:
        print("FAIL ✗ overlap between bad and vertical-only fragments")
        print(sorted(overlap))
        failures += 1
    else:
        print("PASS ✓ no overlap between bad and vertical-only fragments")

    for bad_url in [
        "https://assets.apnews.com/83/1f/238ba42a44b79f31af552a46e097/typeshift.svg",
        "https://assets.apnews.com/04/b6/ed98f9004995bc7af2a363e88ada/memoku.svg",
        "https://assets.apnews.com/example/graphic.png",
    ]:
        if clean_extracted_image_url(bad_url) is None:
            print("PASS ✓ graphic asset rejected at extraction")
        else:
            print("FAIL ✗ graphic asset was not rejected", bad_url)
            failures += 1

    ap_a = (
        "https://dims.apnews.com/dims4/default/abc/2147483647/"
        "strip/true/resize/1200x800!/format/webp/quality/90/"
        "?url=https://assets.apnews.com/37/ec/example.jpg"
    )
    ap_b = "https://assets.apnews.com/37/ec/example.jpg"
    if canonical_image_key(ap_a) == canonical_image_key(ap_b):
        print("PASS ✓ AP resized URLs dedupe correctly")
    else:
        print("FAIL ✗ AP dedupe failed")
        failures += 1

    generic_sources = "\n".join(RSS_FEEDS + DIRECT_IMAGE_PAGES).lower()
    banned_source_terms = ["entertainment", "sports", "technology", "science_and_environment"]
    leaked = [term for term in banned_source_terms if term in generic_sources]
    if leaked:
        print("FAIL ✗ generic-heavy source leaked back in:", leaked)
        failures += 1
    else:
        print("PASS ✓ generic-heavy sources are excluded")

    total_checks = len(TESTS) + 6
    print()
    print(f"{total_checks - failures} passed, {failures} failed")
    print()
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
