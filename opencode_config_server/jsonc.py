"""Minimal JSONC reader: JSON plus comments and trailing commas.

IT edits the corporate configuration by hand, so comments have to survive. The
harness itself parses its config with `jsonc-parser` (allowTrailingComma), and
this module accepts the same dialect so a document that works here works there.
"""

from __future__ import annotations

import json
from typing import Any


def strip(text: str) -> str:
    """Remove `//` and `/* */` comments, honouring string literals."""
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]

        if char == '"':
            # Copy the whole string literal verbatim, escapes included.
            out.append(char)
            i += 1
            while i < length:
                current = text[i]
                out.append(current)
                i += 1
                if current == "\\" and i < length:
                    out.append(text[i])
                    i += 1
                    continue
                if current == '"':
                    break
            continue

        if char == "/" and i + 1 < length:
            following = text[i + 1]
            if following == "/":
                while i < length and text[i] != "\n":
                    i += 1
                continue
            if following == "*":
                i += 2
                while i < length and not (text[i] == "*" and i + 1 < length and text[i + 1] == "/"):
                    # Keep newlines so error line numbers stay meaningful.
                    if text[i] == "\n":
                        out.append("\n")
                    i += 1
                i += 2
                continue

        out.append(char)
        i += 1
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    out: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        char = text[i]
        if char == '"':
            out.append(char)
            i += 1
            while i < length:
                current = text[i]
                out.append(current)
                i += 1
                if current == "\\" and i < length:
                    out.append(text[i])
                    i += 1
                    continue
                if current == '"':
                    break
            continue
        if char == ",":
            j = i + 1
            while j < length and text[j] in " \t\r\n":
                j += 1
            if j < length and text[j] in "}]":
                i += 1
                continue
        out.append(char)
        i += 1
    return "".join(out)


def loads(text: str) -> Any:
    """Parse a JSONC document."""
    return json.loads(_drop_trailing_commas(strip(text)))
