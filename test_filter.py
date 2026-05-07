from server import (
    url_is_known_bad,
    url_is_vertical_only,
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
            failures += 1

    print()
    print(f"{len(TESTS) - failures} passed, {failures} failed")


if __name__ == "__main__":
    main()

