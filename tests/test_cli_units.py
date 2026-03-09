"""Unit tests for CLI helpers."""

from __future__ import annotations

from hw2font.cli import _parse_weights


def test_parse_weights_default():
    assert _parse_weights({}) == [{"name": "Regular", "value": 400, "stroke_delta": 0}]


def test_parse_weights_from_config():
    cfg = {
        "weights": [
            {"name": "Light", "value": 300, "stroke_delta": -1},
            {"name": "Regular", "value": 400, "stroke_delta": 0},
            {"name": "Bold", "value": 700, "stroke_delta": 2},
        ]
    }
    weights = _parse_weights(cfg)
    assert len(weights) == 3
    assert weights[0] == {"name": "Light", "value": 300, "stroke_delta": -1}
    assert weights[2] == {"name": "Bold", "value": 700, "stroke_delta": 2}
