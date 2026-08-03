"""Inspect indentation characters on selected lines of an NC source file."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Show indentation details for selected NC source lines.")
    parser.add_argument("path", type=Path, help="Path to an .nc source file")
    parser.add_argument("lines", nargs="+", type=int, help="One or more 1-based line numbers")
    args = parser.parse_args()

    path = args.path.expanduser().resolve()
    if not path.is_file():
        parser.error(f"file not found: {path}")
    source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    for line_number in sorted(set(args.lines)):
        if not 1 <= line_number <= len(source_lines):
            print(f"Line {line_number}: outside file (file has {len(source_lines)} lines)")
            continue
        text = source_lines[line_number - 1]
        prefix = text[: len(text) - len(text.lstrip())]
        print(f"Line {line_number}: {text!r}")
        print("Indentation:", [(character, ord(character)) for character in prefix], "length=", len(prefix))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
