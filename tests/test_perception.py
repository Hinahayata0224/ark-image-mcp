"""Tests for perception/spatial helpers (offline, no API calls)."""
import io

import server


def test_parse_bbox_basic():
    assert server._parse_bbox("[100,200,300,400]") == [100, 200, 300, 400]
    assert server._parse_bbox("{\"bbox\": [0, 0, 999, 999]}") == [0, 0, 999, 999]
    assert server._parse_bbox("no bbox here") is None


def test_region_to_mask_valid():
    # 1000x1000 image, region [0,0,500,1000] -> left half white
    mask = server._region_to_mask((1000, 1000), "[0,0,500,1000]")
    assert mask is not None
    img = server.PILImage.open(io.BytesIO(mask))
    assert img.size == (1000, 1000)
    assert img.mode == "L"
    px = img.load()
    assert px[250, 500] == 255   # left half: white (edit)
    assert px[750, 500] == 0     # right half: black (preserve)


def test_region_to_mask_invalid():
    assert server._region_to_mask((1000, 1000), "not-a-region") is None
    assert server._region_to_mask((1000, 1000), "[500,500,100,100]") is None  # x2<x1


def test_region_to_mask_clamps():
    # region out of range clamps to image bounds
    mask = server._region_to_mask((1000, 1000), "[0,0,2000,2000]")
    assert mask is not None
    img = server.PILImage.open(io.BytesIO(mask))
    px = img.load()
    assert px[999, 999] == 255  # clamped corner still in white


def test_parse_json_obj():
    assert server._parse_json_obj('{"a": 1}') == {"a": 1}
    assert server._parse_json_obj('```json\n{"passed": true}\n```') == {"passed": True}
    assert server._parse_json_obj('prefix {"passed": false} suffix') == {"passed": False}
    assert server._parse_json_obj("no json") is None
