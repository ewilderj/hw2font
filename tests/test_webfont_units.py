"""Unit tests for webfont conversion helpers."""

from __future__ import annotations

from pathlib import Path

from hw2font.webfont import _css_font_face, _join_url_prefix, generate_webfont


class _FakeNameRecord:
    def __init__(self, name_id: int, text: str):
        self.nameID = name_id
        self._text = text

    def toUnicode(self) -> str:
        return self._text


class _FakeTTFont:
    saved: list[tuple[str | Path, str | None]] = []

    def __init__(self, path: str):
        self.path = path
        self.flavor: str | None = None
        self.tables = {
            "name": type(
                "NameTable",
                (),
                {"names": [_FakeNameRecord(1, "Fake Family")]},
            )(),
        }

    def __getitem__(self, key: str):
        return self.tables[key]

    def save(self, path: str) -> None:
        Path(path).write_bytes(f"saved:{self.flavor}".encode())
        _FakeTTFont.saved.append((path, self.flavor))


def test_join_url_prefix():
    assert _join_url_prefix(".", "font.woff2") == "font.woff2"
    assert _join_url_prefix("/fonts", "font.woff2") == "/fonts/font.woff2"


def test_css_font_face_renders_multiple_sources():
    css = _css_font_face(
        "My Font",
        [("font.woff2", "woff2"), ("font.woff", "woff")],
    )
    assert 'font-family: "My Font";' in css
    assert 'url("font.woff2") format("woff2")' in css
    assert 'url("font.woff") format("woff")' in css


def test_generate_webfont_emits_assets_and_css(tmp_path: Path, monkeypatch):
    input_font = tmp_path / "sample.otf"
    input_font.write_bytes(b"fake-font")
    monkeypatch.setattr("hw2font.webfont.TTFont", _FakeTTFont)
    _FakeTTFont.saved = []

    result = generate_webfont(
        input_font,
        output_dir=tmp_path / "web",
        url_prefix="/assets/fonts",
        emit_woff=True,
    )

    assert result["family_name"] == "Fake Family"
    assert [p.suffix for p in result["files"]] == [".woff2", ".woff"]
    assert Path(result["css_path"]).exists()
    css = Path(result["css_path"]).read_text()
    assert '/assets/fonts/sample.woff2' in css
    assert '/assets/fonts/sample.woff' in css
    assert _FakeTTFont.saved == [
        (str(tmp_path / "web" / "sample.woff2"), "woff2"),
        (str(tmp_path / "web" / "sample.woff"), "woff"),
    ]


def test_css_font_face_with_weight():
    css = _css_font_face(
        "My Font",
        [("font.woff2", "woff2")],
        font_weight="700",
    )
    assert "font-weight: 700;" in css


def test_generate_webfont_passes_font_weight(tmp_path: Path, monkeypatch):
    input_font = tmp_path / "sample.otf"
    input_font.write_bytes(b"fake-font")
    monkeypatch.setattr("hw2font.webfont.TTFont", _FakeTTFont)
    _FakeTTFont.saved = []

    result = generate_webfont(
        input_font,
        output_dir=tmp_path / "web",
        emit_woff=False,
        font_weight="700",
    )
    assert "font-weight: 700;" in result["css"]
