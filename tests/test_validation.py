"""Size validation tests (offline, no API calls)."""
import server


def test_preset_sizes_valid():
    for size in ["1K", "2K", "4K"]:
        assert server.is_valid_size(size)


def test_widthxheight_valid():
    assert server.is_valid_size("2048x1366")      # landscape
    assert server.is_valid_size("1366x2048")      # portrait
    assert server.is_valid_size("1024x1536")      # portrait (typical phone)
    assert server.is_valid_size("1536x1152")      # 4:3-ish
    assert server.is_valid_size("1280x853")


def test_area_minimum_enforced():
    # area < 921600 px is rejected even if each side is in range
    assert not server.is_valid_size("1024x683")   # 699392 px
    assert not server.is_valid_size("800x800")    # 640000 px


def test_bounds_enforced():
    assert not server.is_valid_size("500x500")    # side < 512
    assert not server.is_valid_size("5000x1000")  # side > 4096
    assert not server.is_valid_size("99999x10")


def test_landscape_presets_rejected():
    # Seedream 5.0 does not accept these legacy presets
    assert not server.is_valid_size("2K-landscape")
    assert not server.is_valid_size("1K-portrait")


def test_garbage_rejected():
    assert not server.is_valid_size("abc")
    assert not server.is_valid_size("")
    assert not server.is_valid_size("2Kx2K")
    assert not server.is_valid_size("2048,1366")


def test_ark_generate_image_invalid_size_returns_err():
    out = server.ark_generate_image("a cat", size="999K")
    assert out["ok"] is False
    assert out["error_type"] == "invalid_argument"


def test_ark_generate_image_empty_prompt_returns_err():
    out = server.ark_generate_image("   ")
    assert out["ok"] is False
    assert out["error_type"] == "invalid_argument"


def test_ark_generate_image_bad_response_format():
    out = server.ark_generate_image("a cat", response_format="xml")
    assert out["ok"] is False
    assert out["error_type"] == "invalid_argument"
