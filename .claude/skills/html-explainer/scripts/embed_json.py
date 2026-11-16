#!/usr/bin/env python3
"""Embed a JSON file into a page's <script type="application/json" id="..."> block.

Use it to put real captured outputs (examples, data series, measured figures) into an
explainer, so the page can be refreshed whenever the source changes and a reviewer can see
that the examples are genuine.

    python embed_json.py page.html --id examples --json captured.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("page", type=Path)
    parser.add_argument("--id", required=True, help="id of the <script type=\"application/json\"> element")
    parser.add_argument("--json", required=True, type=Path, help="JSON file to embed")
    args = parser.parse_args()

    data = json.loads(args.json.read_text(encoding="utf-8"))
    # Compact, and "</" escaped so the data can never close the <script> element early.
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = args.page.read_text(encoding="utf-8")
    pattern = re.compile(r'(<script type="application/json" id="' + re.escape(args.id) + r'">)(.*?)(</script>)', re.S)
    if not pattern.search(html):
        print(f'{args.page}: no <script type="application/json" id="{args.id}"> element', file=sys.stderr)
        return 1
    html = pattern.sub(lambda m: m.group(1) + blob + m.group(3), html, count=1)
    args.page.write_text(html, encoding="utf-8")
    print(f"Embedded {args.json} into #{args.id} ({len(blob) / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
