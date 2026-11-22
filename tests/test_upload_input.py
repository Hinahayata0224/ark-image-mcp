"""Tests for data:/b64: image sources and the POST /upload endpoint (offline)."""
import base64
import asyncio
import io
import json

import pytest
from PIL import Image

import server


def _jpeg_bytes(size=(64, 48), color=(200, 80, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


# ---------- load_input_image ----------

def test_data_url_jpeg_roundtrip():
    raw = _jpeg_bytes()
    data, label = server.load_input_image(
        "data:image/jpeg;base64," + base64.b64encode(raw).decode())
    assert data == raw
    assert label.startswith("data:image/jpeg")


def test_data_url_without_base64_marker_is_percent_decoded():
    data, label = server.load_input_image("data:text/plain,%41%42")
    assert data == b"AB"
    assert "data:text/plain" in label


def test_data_url_malformed_raises():
    with pytest.raises(ValueError, match="malformed data URL"):
        server.load_input_image("data:image/jpeg;base64")


def test_data_url_empty_payload_raises():
    with pytest.raises(ValueError, match="0 bytes"):
        server.load_input_image("data:,")


def test_b64_prefix_decodes():
    raw = _jpeg_bytes()
    data, _ = server.load_input_image("b64:" + base64.b64encode(raw).decode())
    assert data == raw


def test_relative_path_gives_actionable_error():
    """A client-side relative path must fail with a hint, not a URL error."""
    with pytest.raises(ValueError) as exc:
        server.load_input_image("input/image-39606d.jpg")
    msg = str(exc.value)
    assert "not a readable local path" in msg
    assert "/upload" in msg


def test_absolute_missing_path_also_hinted(tmp_path):
    with pytest.raises(ValueError, match="/upload"):
        server.load_input_image(str(tmp_path / "nope.jpg"))


def test_error_message_carries_a_runnable_command(tmp_path, monkeypatch):
    """The model should not have to discover /upload: the error tells it what to run."""
    monkeypatch.setenv("ARK_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setattr(server, "ARK_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setattr(server, "ARK_UPLOAD_TOKEN", "upload-tok")
    with pytest.raises(ValueError) as exc:
        server.load_input_image("input/photo.jpg")
    msg = str(exc.value)
    assert "https://mcp.example.com/upload" in msg
    assert "Bearer upload-tok" in msg
    assert "--data-binary" in msg


def test_instructions_lead_with_the_upload_workflow(monkeypatch):
    """A client that skim-reads instructions must still hit the upload path."""
    text = server.mcp.instructions
    assert "READ THIS FIRST" in text
    lead = text[: text.index("READ THIS FIRST")]
    assert "ark_scene_profile" not in lead  # tools listing comes after the warning
    assert "/upload" in text[:1400]


# ---------- transparency ----------

def test_rgba_png_downscales_to_jpeg_instead_of_raising():
    """Phones and screenshots produce RGBA PNGs; JPEG cannot store alpha."""
    buf = io.BytesIO()
    Image.new("RGBA", (120, 90), (255, 0, 0, 128)).save(buf, format="PNG")
    uri = server._downscale_uri(buf.getvalue())
    assert uri.startswith("data:image/jpeg;base64,")


def test_rgba_png_survives_display_reencode():
    buf = io.BytesIO()
    Image.new("RGBA", (120, 90), (0, 0, 255, 200)).save(buf, format="PNG")
    out = server._to_display_jpeg(buf.getvalue())
    assert out[:2] == b"\xff\xd8"  # JPEG SOI


def test_flatten_composites_alpha_on_white():
    img = Image.new("RGBA", (10, 10), (255, 0, 0, 0))  # fully transparent red
    flat = server._flatten_to_rgb(img)
    assert flat.mode == "RGB"
    assert flat.getpixel((0, 0)) == (255, 255, 255)


# ---------- POST /upload ----------

def _post_upload(body: bytes):
    """Drive the upload handler with a minimal fake Starlette Request."""
    class _Req:
        def __init__(self, body):
            self._body = body
            self.headers = {}

        async def body(self):
            return self._body

    resp = asyncio.run(server._handle_upload(_Req(body)))
    return resp.status_code, json.loads(resp.body)


def test_upload_writes_file_and_returns_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    status, payload = _post_upload(_jpeg_bytes())
    assert status == 200
    assert payload["ok"] is True
    assert payload["path"].startswith(str(tmp_path))
    assert payload["path"].endswith(".jpg")
    assert payload["bytes"] == len(_jpeg_bytes())


def test_upload_rejects_non_image(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    status, payload = _post_upload(b"this is definitely not a jpeg")
    assert status == 400
    assert payload["ok"] is False
    assert "not a decodable image" in payload["error"]


def test_upload_rejects_empty_body(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    status, payload = _post_upload(b"")
    assert status == 400


def test_upload_enforces_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    monkeypatch.setattr(server, "ARK_MAX_UPLOAD_MB", 1)
    status, payload = _post_upload(b"x" * (1024 * 1024 + 10))
    assert status == 413
    assert "exceeds" in payload["error"]


def test_upload_preserves_png_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (1, 2, 3)).save(buf, format="PNG")
    status, payload = _post_upload(buf.getvalue())
    assert status == 200
    assert payload["path"].endswith(".png")


# ---------- per-path token routing ----------

def _scope(path, token=None, query=b""):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return {"type": "http", "path": path, "headers": headers, "query_string": query}


def _run(scope, upload_token="upload-only"):
    seen = {}

    async def app(s, r, se):
        seen["reached"] = True

    async def send(msg):
        if msg["type"] == "http.response.start":
            seen["status"] = msg["status"]

    async def receive():
        return {"type": "http.request"}

    mw = server._BearerAuthMiddleware(app, "main-token", upload_token)
    asyncio.run(mw(scope, receive, send))
    return seen


def test_upload_accepts_dedicated_upload_token():
    assert _run(_scope("/upload", "upload-only")).get("reached") is True


def test_upload_also_accepts_main_token():
    assert _run(_scope("/upload", "main-token")).get("reached") is True


def test_upload_rejects_unknown_token():
    assert _run(_scope("/upload", "nope"))["status"] == 401


def test_mcp_route_rejects_upload_token():
    """The upload token must not be usable for tool calls."""
    assert _run(_scope("/mcp", "upload-only"))["status"] == 401


def test_mcp_route_accepts_main_token():
    assert _run(_scope("/mcp", "main-token")).get("reached") is True
