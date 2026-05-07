from server import (
    KNOWN_BAD_URL_FRAGMENTS,
    VERTICAL_ONLY_URL_FRAGMENTS,
    url_is_known_bad,
    url_is_vertical_only,
    canonical_image_key,
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
    (
        "https://ichef.bbci.co.uk/images/ic/1024x576/p0ngd4cc.jpg",
        True,
        False,
        "known bad cropped image should be rejected",
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
            print(" ", url)
            print("   expected bad:", expected_bad, "got:", got_bad)
            print("   expected vertical:", expected_vertical, "got:", got_vertical)
            failures += 1

    overlap = set(KNOWN_BAD_URL_FRAGMENTS) & set(VERTICAL_ONLY_URL_FRAGMENTS)

    if overlap:
        print("FAIL ✗ overlap between bad and vertical-only fragments")
        print(sorted(overlap))
        failures += 1
    else:
        print("PASS ✓ no overlap between bad and vertical-only fragments")

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

    print()
    print(f"{len(TESTS) + 2 - failures} passed, {failures} failed")
    print()


if __name__ == "__main__":
    main()
