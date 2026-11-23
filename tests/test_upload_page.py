"""Tests for the browser upload page and the recent-uploads tool (offline)."""
import asyncio
import io
import json
import os

from PIL import Image

import server


def _jpeg(size=(80, 60)):
    buf = io.BytesIO()
    Image.new("RGB", size, (12, 200, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _get(handler, token="tok"):
    class _Req:
        def __init__(self):
            self.query_params = {"token": token}

    return asyncio.run(handler(_Req()))


# ---------- page ----------

def test_page_serves_html_with_token_baked_in(monkeypatch):
    monkeypatch.setattr(server, "ARK_UPLOAD_TOKEN", "page-token")
    resp = _get(server._handle_upload_page)
    assert resp.status_code == 200
    body = resp.body.decode()
    assert "上传图片" in body
    assert "page-token" in body
    assert "null" not in body.split("const TOKEN =")[1].split(";")[0]


def test_page_falls_back_to_main_token(monkeypatch):
    monkeypatch.setattr(server, "ARK_UPLOAD_TOKEN", "")
    monkeypatch.setattr(server, "ARK_MCP_TOKEN", "main-token")
    body = _get(server._handle_upload_page).body.decode()
    assert "main-token" in body


def test_page_has_no_unescaped_placeholder():
    body = _get(server._handle_upload_page).body.decode()
    assert "__TOKEN__" not in body


def test_page_is_mobile_viewport_ready():
    body = _get(server._handle_upload_page).body.decode()
    assert 'name="viewport"' in body
    assert 'accept="image/*"' in body


# ---------- recent ----------

def test_recent_lists_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    older = tmp_path / "older.jpg"
    older.write_bytes(_jpeg())
    os.utime(older, (1000, 1000))
    newer = tmp_path / "newer.jpg"
    newer.write_bytes(_jpeg())
    os.utime(newer, (2000, 2000))

    out = server.ark_recent_uploads(5)
    assert out["ok"] is True
    assert [f["filename"] for f in out["files"]][:2] == ["newer.jpg", "older.jpg"]
    assert out["files"][0]["path"].endswith("newer.jpg")


def test_recent_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    for i in range(6):
        p = tmp_path / f"f{i}.jpg"
        p.write_bytes(_jpeg())
        os.utime(p, (1000 + i, 1000 + i))
    assert len(server.ark_recent_uploads(2)["files"]) == 2


def test_recent_on_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    out = server.ark_recent_uploads()
    assert out["ok"] is True
    assert out["files"] == []
    assert "No uploads yet" in out["hint"]


def test_recent_json_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ARK_INPUT_DIR", str(tmp_path))
    (tmp_path / "a.jpg").write_bytes(_jpeg())
    resp = _get(server._handle_recent)
    payload = json.loads(resp.body)
    assert payload["ok"] is True
    assert payload["files"][0]["filename"] == "a.jpg"


# ---------- token scoping ----------

def test_upload_token_covers_page_and_recent():
    mw = server._BearerAuthMiddleware(None, "main", "upload")
    assert "upload" in mw._accepted("/upload")
    assert "upload" in mw._accepted("/recent")
    assert "upload" not in mw._accepted("/mcp")


def test_main_token_still_covers_everything():
    mw = server._BearerAuthMiddleware(None, "main", "upload")
    for path in ("/upload", "/recent", "/mcp"):
        assert "main" in mw._accepted(path)


def test_instructions_point_at_the_upload_page():
    assert "BEST FOR PHONES" in server.mcp.instructions
    assert "/upload" in server.mcp.instructions


def test_page_url_embeds_token_when_configured(monkeypatch):
    monkeypatch.setattr(server, "ARK_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setattr(server, "ARK_UPLOAD_TOKEN", "abc123")
    assert server.upload_page_url() == "https://mcp.example.com/upload?token=abc123"


def test_page_url_without_token_is_plain(monkeypatch):
    monkeypatch.setattr(server, "ARK_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setattr(server, "ARK_UPLOAD_TOKEN", "")
    monkeypatch.setattr(server, "ARK_MCP_TOKEN", "")
    assert server.upload_page_url() == "https://mcp.example.com/upload"
