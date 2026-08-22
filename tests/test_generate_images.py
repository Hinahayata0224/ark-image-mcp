"""Tests for _generate_images with mocked API responses (offline)."""
import base64
import io
import json

from PIL import Image

import server


class _FakeResp:
    def __init__(self, json_body, status=200):
        self._json = json_body
        self.status_code = status
        self.text = json.dumps(json_body)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            class E(requests.exceptions.HTTPError):
                pass
            raise E(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def _png_bytes(size=(100, 80)):
    img = Image.new("RGB", size, (10, 200, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_generate_images_url_download(monkeypatch, tmp_path):
    png = _png_bytes()
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _FakeResp({"model": "m", "created": 1, "data": [{"url": "https://img/x.png"}],
                          "usage": {"generated_images": 1}})

    def fake_get(url, *a, **k):
        class R:
            content = png
            headers = {"Content-Type": "image/png"}

            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server.requests, "get", fake_get)
    out = server._generate_images({"model": "m", "prompt": "x"}, str(tmp_path))
    assert out["ok"] is True
    assert len(out["files"]) == 1
    assert out["files"][0].endswith(".png")
    assert out["usage"]["generated_images"] == 1
    assert calls["n"] == 1


def test_generate_images_b64_json(monkeypatch, tmp_path):
    png = _png_bytes()
    b64 = base64.b64encode(png).decode()

    def fake_post(*a, **k):
        return _FakeResp({"model": "m", "data": [{"b64_json": b64}]})

    monkeypatch.setattr(server.requests, "post", fake_post)
    out = server._generate_images({"model": "m", "prompt": "x"}, str(tmp_path))
    assert out["ok"] is True
    assert len(out["files"]) == 1
    assert out["files"][0].endswith(".png")


def test_generate_images_api_error(monkeypatch):
    import requests

    def fake_post(*a, **k):
        class E(requests.exceptions.HTTPError):
            def __init__(self):
                super().__init__("500")
                self.response = _FakeResp({"error": "boom"}, status=500)
        raise E()

    monkeypatch.setattr(server.requests, "post", fake_post)
    out = server._generate_images({"model": "m", "prompt": "x"}, ".")
    assert out["ok"] is False
    assert out["error_type"] == "api"


def test_generate_images_empty_data(monkeypatch):
    def fake_post(*a, **k):
        return _FakeResp({"model": "m", "data": []})

    monkeypatch.setattr(server.requests, "post", fake_post)
    out = server._generate_images({"model": "m", "prompt": "x"}, ".")
    assert out["ok"] is True
    assert out["files"] == []


def test_generate_images_download_failure_keeps_url(monkeypatch, tmp_path):
    import requests

    def fake_post(*a, **k):
        return _FakeResp({"data": [{"url": "https://img/x.png"}]})

    def fake_get(url, *a, **k):
        raise requests.exceptions.ConnectionError("dns fail")

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    out = server._generate_images({"model": "m", "prompt": "x"}, str(tmp_path))
    assert out["ok"] is True
    assert out["files"] == []          # nothing saved locally
    assert out["urls"] == ["https://img/x.png"]  # URL preserved so image is reachable
    assert out["errors"]


def test_generate_images_retries_on_5xx(monkeypatch, tmp_path):
    png = _png_bytes()
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            import requests
            class E(requests.exceptions.HTTPError):
                def __init__(self):
                    super().__init__("503")
                    self.response = _FakeResp({"error": "unavailable"}, status=503)
            raise E()
        return _FakeResp({"data": [{"url": "https://img/x.png"}]})

    def fake_get(url, *a, **k):
        class R:
            content = png
            headers = {"Content-Type": "image/png"}

            def raise_for_status(self):
                pass
        return R()

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)  # no real backoff wait
    out = server._generate_images({"model": "m", "prompt": "x"}, str(tmp_path))
    assert out["ok"] is True
    assert calls["n"] == 2
