"""Tests for the optional image-hosting backends (offline; no network calls)."""
import os

import uploader


_PROVIDER_ENV = [
    "ARK_UPLOAD_PROVIDER",
    "ARK_CLOUDINARY_PRESET",
    "ARK_CLOUDINARY_CLOUD",
    "ARK_SMMS_TOKEN",
    "ARK_OSS_ACCESS_KEY_ID",
    "ARK_OSS_ACCESS_KEY_SECRET",
    "ARK_OSS_BUCKET",
    "ARK_OSS_ENDPOINT",
    "ARK_OSS_PREFIX",
    "ARK_OSS_URL_TTL",
]


def _clear(monkeypatch):
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def test_disabled_by_default(monkeypatch):
    _clear(monkeypatch)
    info = uploader.describe_config()
    assert info["enabled"] is False
    assert info["provider"] is None
    # Disabled must be a silent no-op so generation is never affected.
    assert uploader.upload_paths(["/nonexistent/ark_img_x.jpg"]) == ([], [])


def test_unknown_provider_is_reported(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "bogus")
    info = uploader.describe_config()
    assert info["enabled"] is True
    assert info["ok"] is False
    assert "unknown provider" in info["detail"]


def test_aliyun_reports_missing_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "aliyun")
    info = uploader.describe_config()
    assert info["ok"] is False
    for key in ("ARK_OSS_ACCESS_KEY_ID", "ARK_OSS_BUCKET", "ARK_OSS_ENDPOINT"):
        assert key in info["detail"]


def test_aliyun_configured(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "aliyun")
    monkeypatch.setenv("ARK_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("ARK_OSS_ACCESS_KEY_SECRET", "secret")
    monkeypatch.setenv("ARK_OSS_BUCKET", "bucket")
    monkeypatch.setenv("ARK_OSS_ENDPOINT", "https://oss-cn-shenzhen.aliyuncs.com")
    info = uploader.describe_config()
    assert info["ok"] is True
    assert info["detail"] == "configured"


def test_upload_failure_is_non_fatal(monkeypatch, tmp_path):
    """A provider error must land in notes, never propagate to the caller."""
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "smms")
    monkeypatch.setenv("ARK_SMMS_TOKEN", "token")
    image = tmp_path / "ark_img_test.jpg"
    image.write_bytes(b"not really a jpeg")

    def boom(path):
        raise RuntimeError("network unreachable")

    monkeypatch.setitem(uploader._UPLOADERS, "smms", boom)
    urls, notes = uploader.upload_paths([str(image)])
    assert urls == []
    assert any("network unreachable" in n for n in notes)


def test_upload_skips_missing_files(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "smms")
    monkeypatch.setenv("ARK_SMMS_TOKEN", "token")
    monkeypatch.setitem(uploader._UPLOADERS, "smms", lambda p: "https://x/" + p)
    urls, notes = uploader.upload_paths([str(tmp_path / "missing.jpg")])
    assert urls == []
    assert notes == []


def test_upload_collects_urls(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "smms")
    monkeypatch.setenv("ARK_SMMS_TOKEN", "token")
    first = tmp_path / "a.jpg"
    second = tmp_path / "b.jpg"
    first.write_bytes(b"x")
    second.write_bytes(b"y")
    monkeypatch.setitem(uploader._UPLOADERS, "smms", lambda p: f"https://x/{os.path.basename(p)}")
    urls, notes = uploader.upload_paths([str(first), str(second)])
    assert urls == ["https://x/a.jpg", "https://x/b.jpg"]
    assert notes == []


def test_upload_captures_url_even_if_provider_succeeds(monkeypatch, tmp_path):
    """One bad file must not stop the remaining files from uploading."""
    _clear(monkeypatch)
    monkeypatch.setenv("ARK_UPLOAD_PROVIDER", "smms")
    monkeypatch.setenv("ARK_SMMS_TOKEN", "token")
    good = tmp_path / "good.jpg"
    good.write_bytes(b"x")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"y")

    def selective(path):
        if path.endswith("bad.jpg"):
            raise RuntimeError("rejected")
        return "https://x/good.jpg"

    monkeypatch.setitem(uploader._UPLOADERS, "smms", selective)
    urls, notes = uploader.upload_paths([str(good), str(bad)])
    assert urls == ["https://x/good.jpg"]
    assert any("rejected" in n for n in notes)
