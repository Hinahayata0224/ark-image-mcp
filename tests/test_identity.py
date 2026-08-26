"""Tests for identity templates tool (offline; no API calls for validation paths)."""
import server


def test_identity_presets():
    assert len(server.IDENTITY_TEMPLATE_PRESETS) == 5
    assert server.IDENTITY_TEMPLATE_PRESETS[0][0] == "正面半身像"
    assert "腰部" in server.IDENTITY_TEMPLATE_PRESETS[0][1]
    assert "手臂" in server.IDENTITY_TEMPLATE_PRESETS[0][1]
    assert "四十五度" in server.IDENTITY_TEMPLATE_PRESETS[1][0]


def test_count_validation():
    out = server.ark_generate_identity_templates("x.jpg", count=0)
    assert out["ok"] is False
    assert out["error_type"] == "invalid_argument"
    out2 = server.ark_generate_identity_templates("x.jpg", count=6)
    assert out2["ok"] is False


def test_empty_images():
    out = server.ark_generate_identity_templates("")
    assert out["ok"] is False
    assert out["error_type"] == "invalid_argument"


def test_load_failure():
    out = server.ark_generate_identity_templates(r"Z:\nonexistent\a.jpg,Z:\nonexistent\b.jpg")
    assert out["ok"] is False
    assert out["error_type"] == "load"


def test_vision_images_sampling():
    items = list(range(22))
    sampled = server._sample_images(items, 5)
    assert len(sampled) == 5
    # evenly spread: first, last-ish, and middle represented
    assert sampled[0] == 0
    assert len(set(sampled)) == 5
    # no sampling when under limit
    assert server._sample_images(list(range(3)), 5) == [0, 1, 2]


def test_template_presets_waist_and_arms():
    # presets must include waist-framing and full-arms requirement
    assert "腰部" in server.IDENTITY_TEMPLATE_PRESETS[0][1]
    assert "手臂" in server.IDENTITY_TEMPLATE_PRESETS[0][1]
    assert "四十五度" in server.IDENTITY_TEMPLATE_PRESETS[1][0]
