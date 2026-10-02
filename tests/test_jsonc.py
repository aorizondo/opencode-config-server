"""The corporate document is JSONC, like the harness's own config files."""

from __future__ import annotations

import pytest

from opencode_config_server import jsonc


class TestComments:
    def test_strips_line_and_block_comments(self):
        assert jsonc.loads('{\n // one\n "a": 1, /* two */ "b": 2\n}') == {"a": 1, "b": 2}

    def test_keeps_comment_markers_inside_strings(self):
        document = jsonc.loads('{"url": "https://corp.example//path", "glob": "/* not a comment */"}')
        assert document["url"] == "https://corp.example//path"
        assert document["glob"] == "/* not a comment */"

    def test_keeps_escaped_quotes_intact(self):
        assert jsonc.loads('{"q": "a \\" // b"}') == {"q": 'a " // b'}

    def test_preserves_line_numbers_for_errors(self):
        with pytest.raises(ValueError) as error:
            jsonc.loads("{\n/* a\nb\nc */\n  bad\n}")
        assert "line 5" in str(error.value)


class TestTrailingCommas:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"a": [1, 2, ], }', {"a": [1, 2]}),
            ('{"a": {"b": 1,},}', {"a": {"b": 1}}),
            ('{"a": "trailing, comma in string,"}', {"a": "trailing, comma in string,"}),
        ],
    )
    def test_accepts_them(self, text: str, expected: dict):
        assert jsonc.loads(text) == expected


def test_parses_the_shipped_corporate_document():
    from pathlib import Path

    path = Path(__file__).parent.parent / "config" / "opencode.jsonc"
    document = jsonc.loads(path.read_text(encoding="utf-8"))
    assert document["$schema"] == "https://opencode.ai/config.json"
