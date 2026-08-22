"""Image processing tests (offline, no API calls)."""
import base64
import io
import os

import pytest
from PIL import Image

import server


def _make_img_bytes(size=(200, 150), fmt="JPEG", color=(120, 80, 200), exif_orient=None):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif_orient is not None:
        # craft minimal EXIF with orientation
        exif = Image.Exif()
        exif[0x0112] = exif_orient
        img.save(buf, format=fmt, exif=exif)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


def test_small_image_unchanged():
    data = _make_img_bytes()
    out, compressed = server.maybe_compress(data)
    assert compressed is False
    assert out == data


def test_large_image_downscaled():
    # 6000x4000 -> half
    data = _make_img_bytes((6000, 4000))
    out, compressed = server.maybe_compress(data)
    assert compressed is True
    img = Image.open(io.BytesIO(out))
    assert img.size == (3000, 2000)


def test_exif_orientation_applied():
    # raw pixels are landscape, EXIF says rotate 90 CW -> visual should be portrait
    data = _make_img_bytes((3000, 2000), exif_orient=8)
    out, _ = server.maybe_compress(data)
    img = Image.open(io.BytesIO(out))
    # exif_transpose(8) swaps dimensions; no resize because both dims < 5000
    assert img.size == (2000, 3000)


def test_data_uri_mime_jpeg():
    data = _make_img_bytes(fmt="JPEG")
    uri = server.to_base64_data_uri(data)
    assert uri.startswith("data:image/jpeg;base64,")
    # round-trip
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw == data


def test_data_uri_mime_png():
    data = _make_img_bytes(fmt="PNG")
    uri = server.to_base64_data_uri(data)
    assert uri.startswith("data:image/png;base64,")


def test_download_image_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    class Boom(Exception):
        pass

    def fake_get(*a, **k):
        calls["n"] += 1
        raise requests_err()

    def requests_err():
        import requests
        return requests.exceptions.ConnectionError("boom")

    monkeypatch.setattr(server.requests, "get", fake_get)
    with pytest.raises(Exception):
        server.download_image("https://example.com/x.jpg", ".")
    assert calls["n"] == 3  # 3 attempts before giving up


def test_unique_path_suffix(tmp_path):
    p1 = server._unique_path(str(tmp_path), "jpg")
    p2 = server._unique_path(str(tmp_path), "jpg")
    assert p1 != p2
    assert os.path.basename(p1).startswith("ark_img_")
    assert p1.endswith(".jpg")


def test_sniff_ext_from_content_type():
    assert server._sniff_ext(b"", "image/jpeg") == "jpg"
    assert server._sniff_ext(b"", "image/png") == "png"
    assert server._sniff_ext(b"", "image/webp") == "webp"
    # no content type -> sniff via PIL
    jpg = _make_img_bytes(fmt="JPEG")
    assert server._sniff_ext(jpg, "") == "jpg"
    png = _make_img_bytes(fmt="PNG")
    assert server._sniff_ext(png, "") == "png"


def test_save_b64_data(tmp_path):
    jpg = _make_img_bytes(fmt="JPEG")
    b64 = base64.b64encode(jpg).decode()
    path = server._save_b64_data(b64, str(tmp_path))
    assert os.path.exists(path)
    assert path.endswith(".jpg")
