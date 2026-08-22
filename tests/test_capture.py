"""Tests for screen capture (offline; capture itself only on platforms with ImageGrab)."""
import server


def test_capture_screen_region_parse():
    # region parsing is reused from bbox parsing
    assert server._parse_bbox("[0,0,100,100]") == [0, 0, 100, 100]


def test_capture_screen_unsupported_platform(monkeypatch):
    # Simulate a platform where capture returns None -> clear error, no crash
    monkeypatch.setattr(server, "_capture_screen_region", lambda region=None: None)
    out = server.ark_capture_screen()
    assert out["ok"] is False
    assert out["error_type"] == "api"
    assert "not supported" in out["error"]
