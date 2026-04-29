"""
Regression test for the BBC branding filter in server.py.

Run with:
    ./venv/bin/python3 test_filter.py

Each bad_sample_* file is a real BBC-served image that slipped through the filter
at some point. This script confirms they are all still caught after any changes.
raw_bbc_sample.jpg is a confirmed GOOD news photo — it must never be rejected.

When you add a new rule to image_has_bbc_branding(), add the new bad sample here
and re-run to make sure everything still passes.
"""

import sys
import cv2
import numpy as np
from server import image_has_bbc_branding, image_is_probably_full_graphic_page

# (filename, should_be_rejected, description, source_url)
SAMPLES = [
    (
        "bad_sample_1.jpg", True,
        "BBC INDEPTH graphic — mushroom collage with BBC logo",
        "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/1863/live/2b37c9a0-da84-11f0-b67b-690eb873de1b.jpg",
    ),
    (
        "bad_sample_2.jpg", True,
        "BBC VERIFY — war/conflict photo with branding overlay",
        "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/db29/live/5c328660-2f45-11f1-934f-036468834728.jpg",
    ),
    (
        "bad_sample_3.jpg", False,
        "UNCONFIRMED — two people side by side, no visible BBC logo (may not need filtering)",
        "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/ba3a/live/4954dd00-3791-11f1-879d-1b2f5c3919b8.jpg",
    ),
    (
        "bad_sample_4.png", True,
        "BBC VERIFY — map/geography graphic with BBC logo",
        "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/8b21/live/5f2f50e0-3351-11f1-8606-05fe34b06e1b.png",
    ),
    (
        "bad_sample_5.jpg", True,
        "NEWSCAST",
        "https://ichef.bbci.co.uk/images/ic/1008x567/p0l7jnbt.jpg",
    ),
    (
        "bbc.png", True,
        "BBC orb/glow — near-black frame with warm glowing circle",
        "(saved from browser — no original URL)",
    ),
    (
        "raw_bbc_sample.jpg", False,
        "GOOD PHOTO — should never be rejected (person at red carpet event)",
        "https://ichef.bbci.co.uk/ace/standard/240/cpsprodpb/1a44/live/717b0680-3816-11f1-9d5c-8ba507d7dbde.jpg",
    ),
]


def check(path):
    with open(path, "rb") as f:
        data = f.read()
    return image_has_bbc_branding(data) or image_is_probably_full_graphic_page(data)


passed = 0
failed = 0

print()
for filename, should_reject, description, url in SAMPLES:
    try:
        rejected = check(filename)
    except Exception as e:
        print(f"  ERROR  {filename}: {e}")
        failed += 1
        continue

    if should_reject is None:
        # Unconfirmed — just report, don't fail
        status = "REJECTED" if rejected else "passes"
        print(f"  ?      {filename} [{status}] — {description}")
        continue

    ok = rejected == should_reject
    if ok:
        label = "REJECT ✓" if should_reject else "PASS   ✓"
        print(f"  {label}  {filename} — {description}")
        passed += 1
    else:
        label = "MISSED ✗" if should_reject else "FALSE+ ✗"
        print(f"  {label}  {filename} — {description}")
        print(f"           {url}")
        failed += 1

print()
print(f"  {passed} passed, {failed} failed")
print()

sys.exit(1 if failed > 0 else 0)
