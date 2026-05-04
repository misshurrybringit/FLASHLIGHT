"""
Regression checks for the BBC flashlight server rules.
Run with:
    python3 test.py

This does not download images. It checks the URL-fragment rules that decide
whether images are hard-blocked or only allowed on vertical phones.
"""

from server import (
    KNOWN_BAD_URL_FRAGMENTS,
    VERTICAL_ONLY_URL_FRAGMENTS,
    url_is_known_bad,
    url_is_vertical_only,
    url_needs_voice_crop,
)

TESTS = [
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/dce9/live/166137e0-3f11-11f1-bd52-e755d604ece4.jpg",
        False,
        True,
        False,
        "vertical/cropped image should be vertical-phone only, not hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/627b/live/3600d2f0-2214-11f1-b297-95b0a0a8331e.jpg",
        False,
        True,
        False,
        "cropped full-body/vertical image should be vertical-phone only, not hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/1e87/live/9a3df7e0-4562-11f1-b55d-0f258dce1735.jpg",
        False,
        True,
        False,
        "vertical/cropped editorial image should be vertical-phone only, not hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/d379/live/9f35a8f0-4545-11f1-8ea3-630273c214ab.jpg",
        True,
        False,
        False,
        "promotional / graphic editorial image should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/4448/live/f16b6b80-43d5-11f1-bf3e-3d07e81b01ce.jpg",
        False,
        False,
        True,
        "VOICE-logo image should be cropped, not rejected",
    ),
    (
        "https://ichef.bbci.co.uk/images/ic/1024x576/p0ngd4cc.jpg",
        True,
        False,
        False,
        "known bad cropped/isolated image should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/6a8f/live/00a03cc0-3d2d-11f1-9d5c-8ba507d7dbde.jpg",
        True,
        False,
        False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/639a/live/929fd780-43d5-11f1-bf3e-3d07e81b01ce.jpg",
        True,
        False,
        False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
    (
        "https://ichef.bbci.co.uk/ace/standard/1024/cpsprodpb/68be/live/06449360-4525-11f1-bd52-e755d604ece4.jpg",
        True,
        False,
        False,
        "generic editorial portrait / isolated subject should be hard blocked",
    ),
]


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
    if overlap:
        print(f"  FAIL ✗ fragments cannot be both hard-bad and vertical-only: {sorted(overlap)}")
        failures += 1
    else:
        print("  PASS ✓ no overlap between hard-bad and vertical-only fragments")

    print()
    print(f"  {len(TESTS) + 1 - failures} passed, {failures} failed")
    print()
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
