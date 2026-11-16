#!/usr/bin/env python3
"""Make a copy of a standalone explainer that can be published as a claude.ai Artifact.

The Artifact host supplies its own <!doctype>, <html>, <head> and <body>, so this keeps only
what sits between the <!--PAGE:HEAD--> and <!--PAGE:BODY--> markers. The standalone file is
left untouched; it remains the version to send around and open offline.

    python make_fragment.py page.html -o page.artifact.html
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("page", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    html = args.page.read_text(encoding="utf-8")
    parts = re.findall(r"<!--PAGE:(HEAD|BODY)-->(.*?)<!--/PAGE:\1-->", html, flags=re.S)
    if [name for name, _ in parts] != ["HEAD", "BODY"]:
        print(f"{args.page}: needs one <!--PAGE:HEAD--> block and one <!--PAGE:BODY--> block, in that order", file=sys.stderr)
        return 1
    fragment = "\n".join(body.strip("\n") for _, body in parts) + "\n"
    head_end = fragment.find("<style")
    if "<title>" not in fragment[: max(head_end, 0) or 8192]:
        print("warning: put <title> before <style> so hosts that scan only the first 8 KB can find it", file=sys.stderr)
    args.output.write_text(fragment, encoding="utf-8")
    print(f"Wrote {args.output} ({len(fragment) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
