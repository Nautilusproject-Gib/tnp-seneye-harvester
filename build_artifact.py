#!/usr/bin/env python3
"""Produce a single self-contained copy of the dashboard with the data inlined.

Useful for sharing a snapshot (e.g. as a Claude artifact, or an e-mail
attachment) without a web server. The page itself is unchanged - it simply
finds window.NURSERY_DATA already set and skips the fetch.

    python tools/build_artifact.py --out build/nursery-snapshot.html
    python tools/build_artifact.py --strip-skeleton --out build/artifact.html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=os.path.join(ROOT, "dashboard", "index.html"))
    ap.add_argument("--data", default=os.path.join(ROOT, "dashboard", "data", "nursery.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "nursery-snapshot.html"))
    ap.add_argument(
        "--strip-skeleton",
        action="store_true",
        help="drop the doctype/html/head/body wrapper (for hosts that supply their own)",
    )
    args = ap.parse_args()

    with open(args.page, encoding="utf-8") as fh:
        html = fh.read()
    with open(args.data, encoding="utf-8") as fh:
        data = fh.read()

    payload = f"<script>window.NURSERY_DATA={data};</script>\n"
    # must run before the dashboard's own script, which reads the global
    marker = "<script>\n(function ()"
    if marker in html:
        html = html.replace(marker, payload + marker, 1)
    elif "</body>" in html:
        html = html.replace("</body>", payload + "</body>", 1)
    else:
        html = payload + html

    if args.strip_skeleton:
        html = re.sub(r"(?is)^.*?<head>", "", html, count=1)
        html = html.replace("</head>", "", 1)
        html = re.sub(r"(?is)<body[^>]*>", "", html, count=1)
        html = html.replace("</body>", "").replace("</html>", "")
        html = re.sub(r'(?is)<meta[^>]*charset[^>]*>|<meta[^>]*viewport[^>]*>', "", html)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)
    size = os.path.getsize(args.out) / 1024
    print(f"wrote {args.out} ({size:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
