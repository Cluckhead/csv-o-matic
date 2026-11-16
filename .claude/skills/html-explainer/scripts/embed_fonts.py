#!/usr/bin/env python3
"""Embed the IBM Plex faces into an explainer page as base64 WOFF2.

Replaces everything between /*FONTS:START*/ and /*FONTS:END*/ in the page's <style> with
@font-face rules for IBM Plex Sans 400, IBM Plex Sans 600 and IBM Plex Mono 400, preceded by
the SIL Open Font License notice. Safe to run more than once.

    python embed_fonts.py page.html            # in place
    python embed_fonts.py page.html -o out.html
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

FONTS = Path(__file__).resolve().parents[1] / "assets" / "fonts"
START, END = "/*FONTS:START*/", "/*FONTS:END*/"
FACES = [
    ("IBM Plex Sans", 400, "IBMPlexSans-400.woff2"),
    ("IBM Plex Sans", 600, "IBMPlexSans-600.woff2"),
    ("IBM Plex Mono", 400, "IBMPlexMono-400.woff2"),
]
# The Latin subset these files cover (Basic Latin, Latin-1, general punctuation, euro, minus).
UNICODE_RANGE = ("U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, "
                 "U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD")


def font_css() -> str:
    notice = (
        "/* IBM Plex Sans and IBM Plex Mono. Copyright 2019 IBM Corp. Licensed under the SIL Open Font\n"
        "   License, Version 1.1 (https://openfontlicense.org). Embedded unmodified as WOFF2, Latin subset. */"
    )
    rules = [notice]
    for family, weight, filename in FACES:
        data = base64.b64encode((FONTS / filename).read_bytes()).decode("ascii")
        rules.append(
            f'@font-face {{ font-family: "{family}"; font-style: normal; font-weight: {weight}; font-display: swap; '
            f"src: url(data:font/woff2;base64,{data}) format(\"woff2\"); unicode-range: {UNICODE_RANGE}; }}"
        )
    return "\n".join(rules)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("page", type=Path)
    parser.add_argument("-o", "--output", type=Path, help="Write here instead of in place")
    args = parser.parse_args()
    html = args.page.read_text(encoding="utf-8")
    if START not in html or END not in html:
        print(f"{args.page}: no {START} ... {END} block. Copy it from assets/template.html.", file=sys.stderr)
        return 1
    head, rest = html.split(START, 1)
    _, tail = rest.split(END, 1)
    out = head + START + "\n" + font_css() + "\n" + END + tail
    (args.output or args.page).write_text(out, encoding="utf-8")
    print(f"Embedded {len(FACES)} font faces ({len(out) - len(html):+,} bytes) into {args.output or args.page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
