import base64
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

from pydantic import BaseModel, Field

import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "ark-image-gen",
    instructions=(
        "MCP server for 火山方舟 (Volcano Ark) Doubao-Seedream image generation and editing.\n\n"
        "Two models power this server (handled internally):\n"
        "- Seedream 5.0 pro: generates/edits images (image-to-image).\n"
        "- Doubao Seed 2.1 pro (vision): reads reference images and rewrites edit intent into detailed "
        "faithful English prompts; also builds scene profiles and revision prompts.\n\n"
        "TOOLS and recommended workflow:\n"
        "1. ark_scene_profile(images, focus): analyze MULTIPLE photos of the same scene/person into one "
        "structured scene profile (character, outfit, per-photo framing/crop range, background, lighting, "
        "cross-photo details). Build it once per scene, then reuse the returned text across edits.\n"
        "2. ark_edit_image(intent, image, size, watermark, scene_profile): edit a single reference image. "
        "The vision model rewrites your intent (natural language, e.g. 'enlarge the girl's bust slightly; "
        "keep clothing structure and colors unchanged') into a detailed prompt, then Seedream generates. "
        "Pass scene_profile for scene consistency. For OUTPAINTING (extending the canvas), say explicitly "
        "which edge to extend and how far (e.g. 'extend downward to reveal the waist to lower abdomen').\n"
        "3. ark_revise_image(revision, image, reference, size, watermark, scene_profile): iterate on a "
        "previous result from user feedback. Pass the PREVIOUS RESULT as image (it already contains the "
        "correct content), the original photo as reference, and list EVERY issue in revision explicitly "
        "(position/shape/texture); everything not mentioned stays identical to the current result.\n"
        "4. ark_generate_image(prompt, image, size, watermark, response_format): direct generation with a "
        "given prompt; supports 'b64_json' response_format.\n\n"
        "KEY CONSTRAINTS:\n"
        "- size: square presets '1K'/'2K'/'4K' or 'WIDTHxHEIGHT' (each side 512-4096, area >= 921600 px). "
        "Portrait photos are common in this workspace: use e.g. '1024x1536'.\n"
        "- image inputs: local path or URL; comma-separate multiple (mainly for ark_scene_profile). "
        "Images with EXIF rotation are auto-corrected; images >= 5000px are auto-downscaled.\n"
        "- Output images are saved to the caller's current working directory as ark_img_*.jpg; the report "
        "includes files, usage, notes, and refined_prompt (the actual prompt the vision model wrote - "
        "review it to verify intent fidelity).\n"
        "- ALL tools return a JSON object: {\"ok\": true/false, ...}. On success the object carries the "
        "result fields (files, usage, notes, refined_prompt, ...); on failure it carries "
        "{\"ok\": false, \"error_type\": \"config|invalid_argument|load|api\", \"error\": \"message\"}. "
        "ark_scene_profile on success returns {\"ok\": true, \"profile\": \"...\", \"images\": N}."
    ),
)

logger = logging.getLogger("ark-image-gen")
if not logger.handlers:
    handler = logging.StreamHandler()  # stderr; stdio MCP protocol stays clean
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

ARK_API_BASE = "https://ark.cn-beijing.volces.com/api/v3"
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seedream-5-0-pro-260628")
ARK_VISION_MODEL = os.environ.get("ARK_VISION_MODEL", "doubao-seed-2-1-pro-260628")
ARK_TIMEOUT = int(os.environ.get("ARK_TIMEOUT", "300"))

try:
    from PIL import Image as PILImage, ImageDraw, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

MAX_INPUT_DIM = 5000
# Seedream 5.0 accepts square presets and explicit WIDTHxHEIGHT; landscape/portrait
# variants are expressed as WIDTHxHEIGHT (e.g. "2048x1366", "1366x2048").
VALID_SIZES = {"1K", "2K", "4K"}
_SIZE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
MIN_AREA = 921600  # Seedream image editing requires area >= 921600 px
VALID_RESPONSE_FORMATS = {"url", "b64_json"}
UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def is_valid_size(size: str) -> bool:
    if size in VALID_SIZES:
        return True
    if _SIZE_RE.match(size):
        w, h = (int(p) for p in size.split("x"))
        return 512 <= w <= 4096 and 512 <= h <= 4096 and w * h >= MIN_AREA
    return False


def _err(error_type: str, message: str) -> dict:
    """Unified error payload: {"ok": false, "error_type": ..., "error": ...}."""
    return {"ok": False, "error_type": error_type, "error": message,
            "files": None, "urls": None, "usage": None, "notes": None,
            "model": None, "created": None, "refined_prompt": None, "errors": None,
            "profile": None, "images": None}


def _ok(**fields) -> dict:
    """Unified success payload: {"ok": True, **fields}."""
    return {"ok": True, **fields}


class ImageResult(BaseModel):
    """Unified structured output for image generation/editing tools."""
    ok: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    model: Optional[str] = None
    created: Optional[int] = None
    files: Optional[list] = None
    urls: Optional[list] = None
    usage: Optional[dict] = None
    notes: Optional[list] = None
    refined_prompt: Optional[str] = None
    errors: Optional[list] = None


class SceneProfileResult(BaseModel):
    """Structured output for ark_scene_profile."""
    ok: bool
    error_type: Optional[str] = None
    error: Optional[str] = None
    profile: Optional[str] = None
    images: Optional[int] = None


def _unique_path(output_dir: str, ext: str) -> str:
    ts = int(time.time())
    token = uuid.uuid4().hex[:6]
    return os.path.join(output_dir, f"ark_img_{ts}_{token}.{ext}")


def _sniff_ext(img_data: bytes, ct: str = "") -> str:
    """Guess file extension from Content-Type, falling back to PIL and then png."""
    ct_lower = ct.lower()
    if "jpeg" in ct_lower or "jpg" in ct_lower:
        return "jpg"
    if "png" in ct_lower:
        return "png"
    if "webp" in ct_lower:
        return "webp"
    if HAS_PIL:
        try:
            fmt = (PILImage.open(io.BytesIO(img_data)).format or "").lower()
            if fmt == "jpeg":
                return "jpg"
            if fmt in ("png", "webp"):
                return fmt
        except Exception:
            pass
    return "png"


def download_image(url: str, output_dir: str) -> str:
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=UA_HEADERS, timeout=120)
            resp.raise_for_status()
            ext = _sniff_ext(resp.content, resp.headers.get("Content-Type", ""))
            filepath = _unique_path(output_dir, ext)
            with open(filepath, "wb") as f:
                f.write(resp.content)
            return filepath
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning("Download attempt %d/3 failed for %s: %s", attempt + 1, url[:80], e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def _save_b64_data(b64_str: str, output_dir: str) -> str:
    """Decode a base64-encoded image (optionally a data URI) and save it."""
    data = b64_str.strip()
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    raw = base64.b64decode(data)
    ext = _sniff_ext(raw)
    filepath = _unique_path(output_dir, ext)
    with open(filepath, "wb") as f:
        f.write(raw)
    return filepath


def load_input_image(source: str) -> tuple:
    """Load an image from a local path or URL. Returns (img_data, source_label)."""
    try:
        with open(source, "rb") as f:
            img_data = f.read()
        return img_data, os.path.abspath(source)
    except OSError:
        pass
    resp = requests.get(source, headers=UA_HEADERS, timeout=120)
    resp.raise_for_status()
    return resp.content, source


def to_base64_data_uri(img_data: bytes) -> str:
    fmt = "PNG"
    try:
        img = PILImage.open(io.BytesIO(img_data))
        fmt = img.format or "PNG"
    except Exception:
        pass
    mime = "image/png"
    if fmt.upper() in ("JPEG", "JPG"):
        mime = "image/jpeg"
    elif fmt.upper() == "WEBP":
        mime = "image/webp"
    b64 = base64.b64encode(img_data).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def maybe_compress(img_data: bytes) -> tuple:
    """Normalize EXIF orientation (only if rotated), then resize to 1/2 if dimensions >= MAX_INPUT_DIM.

    Returns (img_data, compressed) where compressed indicates whether a resize happened.
    When orientation correction or resize is needed, the result is re-encoded as JPEG
    (uniform, avoids quality drift from format-agnostic re-saves); otherwise the original
    bytes are returned untouched.
    """
    if not HAS_PIL:
        return img_data, False
    try:
        img = PILImage.open(io.BytesIO(img_data))
        orientation = img.getexif().get(0x0112, 1)
        need_transpose = orientation != 1
        if need_transpose:
            img = ImageOps.exif_transpose(img)
        w, h = img.size
        if not need_transpose and w < MAX_INPUT_DIM and h < MAX_INPUT_DIM:
            return img_data, False  # untouched
        if w >= MAX_INPUT_DIM or h >= MAX_INPUT_DIM:
            w, h = w // 2, h // 2
            img = img.resize((w, h), PILImage.LANCZOS)
            need_transpose = True  # mark as modified
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue(), need_transpose
    except Exception:
        return img_data, False


def _post_with_retry(url: str, headers: dict, json_body: dict, timeout: int) -> requests.Response:
    """POST with exponential backoff on connection errors and 429/5xx responses."""
    backoff = [2, 4, 8]
    last_exc = None
    for attempt in range(len(backoff) + 1):
        try:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning("Request attempt %d/%d failed: %s", attempt + 1, len(backoff) + 1, e)
        else:
            if resp.status_code != 429 and not (500 <= resp.status_code < 600):
                return resp
            last_exc = None
            logger.warning(
                "Request attempt %d/%d got HTTP %s, backing off",
                attempt + 1, len(backoff) + 1, resp.status_code,
            )
        if attempt < len(backoff):
            time.sleep(backoff[attempt])
    if last_exc is not None:
        raise last_exc
    return resp


def _chat_with_image(user_text: str, image_data_uri: str, max_tokens: int = 1500,
                     timeout: int = 180) -> str:
    """Call the multimodal chat model (ARK_VISION_MODEL) with a text + image message."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"}
    body = {
        "model": ARK_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_data_uri}},
                {"type": "text", "text": user_text},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    resp = requests.post(f"{ARK_API_BASE}/chat/completions", headers=headers, json=body,
                         timeout=(30, timeout))
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _downscale_uri(img_data: bytes, max_dim: int = 1024, quality: int = 85) -> str:
    """Encode image bytes as a downscaled JPEG data URI (for vision-model calls)."""
    img = PILImage.open(io.BytesIO(img_data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}"


def _chat_with_images(user_text: str, image_uris: list, max_tokens: int = 2000,
                      timeout: int = 300) -> str:
    """Call the multimodal chat model (ARK_VISION_MODEL) with text and multiple images.
    Retries once on 429/timeout (vision service is flaky)."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"}
    body = {
        "model": ARK_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": u}} for u in image_uris]
                     + [{"type": "text", "text": user_text}],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    for attempt in range(2):
        try:
            resp = requests.post(f"{ARK_API_BASE}/chat/completions", headers=headers, json=body,
                                 timeout=(30, timeout))
        except requests.exceptions.RequestException:
            if attempt == 0:  # vision service occasionally times out; retry once
                time.sleep(3)
                continue
            raise
        if resp.status_code == 429 and attempt == 0:
            time.sleep(5)
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    raise RuntimeError("vision chat failed after retries")


@mcp.tool()
def ark_scene_profile(images: str, focus: Optional[str] = None) -> SceneProfileResult:
    """Build a structured scene profile from multiple photos of the same scene/person.

    The multimodal model analyzes the photos together and produces a consolidated
    profile (character, outfit, accessories, framing, background, lighting, and
    cross-photo details). Reuse it across edits of the same scene via
    ark_edit_image(scene_profile=...).

    Args:
        images: Comma-separated local paths or URLs of same-scene photos
        focus: Optional hint for what the profile should emphasize (e.g.
               "the girl's outfit and the classroom background")
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not images or not images.strip():
        return _err("invalid_argument", "images must not be empty.")

    sources = [s.strip() for s in images.split(",")]
    uris = []
    problems = []
    for src in sources:
        try:
            data, _ = load_input_image(src)
            uris.append(_downscale_uri(data))
        except Exception as e:
            problems.append(f"{src}: {e}")
    if not uris:
        return _err("load", f"no images could be loaded: {problems}")
    if problems:
        logger.warning("ark_scene_profile: some images failed to load: %s", problems)

    instruction = (
        "You are a scene builder. Below are multiple photos of the SAME scene/person. "
        "Analyze them together and produce ONE detailed scene profile in Chinese, "
        "organized as sections, to keep future edits of this scene consistent:\n"
        "1) 主体人物: 性别、年龄感、发型发色、五官、表情; 每张图里人物姿势与手部动作的差异分别说明。\n"
        "2) 服装与配饰: 逐件描述款式、颜色、材质、图案; 所有配饰(发夹、手链、手表、戒指等), "
        "并注明每件在哪张图中可见。\n"
        "3) 构图与画面范围: 每张图的景别——画面顶部和底部各切到人物/场景的什么位置"
        "(例如'底部到胸部/腰部/小腹/大腿')、机位角度、人物在画面中的位置。\n"
        "4) 背景环境: 场景、墙面/黑板/涂鸦/道具等细节。\n"
        "5) 光线与色调。\n"
        "6) 跨图互补信息: 哪些细节只在某张图中可见(可用来补全其他图被遮挡的部分)。\n"
        + (f"重点方向: {focus}\n" if focus else "")
        + "只输出画像正文, 不要解释。"
    )
    try:
        profile = _chat_with_images(instruction, uris, max_tokens=2500)
    except Exception as e:
        return _err("api", f"scene profile chat failed: {e}")
    return _ok(profile=profile, images=len(uris))


def refine_edit_prompt(img_data: bytes, intent: str, scene_profile: Optional[str] = None) -> str:
    """Use the multimodal model to rewrite a user's edit intent into a detailed,
    faithful English image-edit prompt, based on the reference image."""
    uri = to_base64_data_uri(img_data)
    instruction = (
        "You are a professional image-editing prompt engineer. The user provides a reference image "
        "and an edit intent; rewrite it into ONE detailed English prompt for an image-to-image "
        "editing model (Seedream / Stable Diffusion class). Requirements:\n"
        "1) Begin by stating that this is an edit of the provided reference photograph, and that all "
        "original subjects, scene composition, lighting, and framing must be preserved.\n"
        "2) Describe the single requested modification with accurate, professional, tasteful wording "
        "(e.g. 'slightly fuller, naturally proportioned bust') - never vulgar language.\n"
        "3) Explicitly state that clothing structure, color, fabric texture and fold direction must "
        "remain unchanged around the modified area.\n"
        "4) Enumerate the key visual elements from the reference image (person features, outfit "
        "details, props, pose, background) so the model reproduces them faithfully.\n"
        "5) Output ONLY the prompt text - no explanations, no prefixes, no quotation marks.\n\n"
        f"[Edit intent] {intent}"
    )
    if scene_profile:
        instruction += (
            "\n\nA scene profile of this same scene/person is provided below. Use it to keep the "
            "scene, person, outfit, and background consistent with the established profile; it may "
            "contain details not visible in the reference image, include them in the prompt when "
            "relevant to the edit.\n"
            f"[Scene profile] {scene_profile}"
        )
    return _chat_with_image(instruction, uri)


def refine_edit_prompt_with_context(img_data: bytes, intent: str,
                                    scene_profile: Optional[str],
                                    context_uris: list) -> str:
    """Like refine_edit_prompt, but also shows additional same-scene photos as
    visual memory to improve scene/outfit consistency."""
    uri = to_base64_data_uri(img_data)
    instruction = (
        "You are a professional image-editing prompt engineer. The FIRST image is the reference "
        "image to edit; the images after it are ADDITIONAL photos of the SAME scene/person used "
        "as visual memory (they may show details not visible in the reference). Rewrite the user's "
        "edit intent into ONE detailed English prompt for image-to-image editing, faithful to the "
        "reference and consistent with the additional photos. Requirements:\n"
        "1) Begin by stating this is an edit of the provided reference photograph, preserving all "
        "original subjects, scene composition, lighting, and framing.\n"
        "2) Describe the single requested modification with accurate, professional, tasteful wording.\n"
        "3) Explicitly state that clothing structure, color, fabric texture and fold direction must "
        "remain unchanged around the modified area.\n"
        "4) Use details from the additional photos (outfit, accessories, background, pose) to make "
        "the reproduction faithful, but do NOT alter the reference image's own content.\n"
        "5) Output ONLY the prompt text - no explanations, no prefixes, no quotation marks.\n\n"
        f"[Edit intent] {intent}"
    )
    if scene_profile:
        instruction += (
            "\n\nA scene profile of this same scene/person is provided below. Use it to keep the "
            "scene and outfit consistent with the established profile.\n"
            f"[Scene profile] {scene_profile}"
        )
    return _chat_with_images(instruction, [uri] + context_uris)


def revise_edit_prompt(result_data: bytes, revision: str,
                       reference_data: Optional[bytes] = None,
                       scene_profile: Optional[str] = None) -> str:
    """Rewrite a revision prompt from user feedback on a previous edit result.

    The first image is the current edited result; reference_data (optional) is the
    original photo, used only to confirm original appearance details.
    """
    images = [_downscale_uri(result_data)]
    if reference_data:
        images.append(_downscale_uri(reference_data))
    instruction = (
        "You are a professional image-editing prompt engineer. The FIRST image is the current edited "
        "result; a SECOND image (if provided) is the original reference photo. The user reviewed the "
        "result and gives revision feedback below. Rewrite ONE detailed English prompt to REGENERATE "
        "the image from the current result, fixing ONLY the issues listed in the revision feedback, "
        "keeping every other aspect of the current result exactly as it is, and keeping the person and "
        "scene consistent with the original reference. Requirements:\n"
        "1) Begin by stating this is a revision of the current result image, preserving all content "
        "that is not explicitly listed as needing a fix.\n"
        "2) For EACH issue in the revision, describe the exact desired fix precisely (position, shape, "
        "texture, etc.).\n"
        "3) Explicitly state that everything NOT mentioned in the revision - person, pose, outfit, "
        "accessories, background, lighting, composition, and the already-correct parts - must stay "
        "identical to the current result.\n"
        "4) Clothing structure, color, fabric texture and fold direction stay unchanged except where "
        "the revision explicitly asks to change them.\n"
        "5) Output ONLY the prompt text - no explanations, no prefixes, no quotation marks.\n\n"
        f"[Revision feedback] {revision}"
    )
    if scene_profile:
        instruction += (
            "\n\nA scene profile of this same scene/person is provided below. Use it to keep the scene "
            "and outfit consistent with the established profile.\n"
            f"[Scene profile] {scene_profile}"
        )
    return _chat_with_images(instruction, images)


# ---------- 感知层：可查询的视觉接口 ----------

def _parse_bbox(text: str) -> Optional[list]:
    """Parse a [x1,y1,x2,y2] bbox from model output (0-1000 normalized)."""
    if not text:
        return None
    # try strict JSON array first (handle quotes/whitespace)
    try:
        arr = json.loads(text.strip())
        if isinstance(arr, list) and len(arr) == 4 and all(isinstance(v, (int, float)) for v in arr):
            return [int(v) for v in arr]
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", text)
    if not m:
        return None
    return [int(m.group(i)) for i in range(1, 5)]


@mcp.tool()
def ark_analyze_image(
    image: str,
    query: str,
    region: Optional[str] = None,
) -> SceneProfileResult:
    """Ask a targeted question about a region of an image (the vision model 'sees' it).

    Unlike a one-shot description, this gives the caller a queryable, region-scoped
    'eye': you can ask about the whole image or a specific region and get a focused
    answer back.

    Args:
        image: Local path or URL of the image
        query: Your question, e.g. "what color is the girl's top?" or
               "describe the pattern on the cup"
        region: Optional region to focus on, as [x1,y1,x2,y2] in 0-1000 normalized
                coordinates (top-left origin), e.g. "[200,100,600,500]"
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not image or not image.strip():
        return _err("invalid_argument", "image is required.")
    if not query or not query.strip():
        return _err("invalid_argument", "query must not be empty.")
    try:
        data, _ = load_input_image(image)
        uri = _downscale_uri(data)
    except Exception as e:
        return _err("load", f"failed to load image {image}: {e}")

    text = query.strip()
    if region:
        text = f"Focus ONLY on the region {region} (0-1000 normalized, top-left origin).\n{text}"
    try:
        answer = _chat_with_image(text, uri, max_tokens=800)
    except Exception as e:
        return _err("api", f"analyze chat failed: {e}")
    return _ok(profile=answer, images=1)


@mcp.tool()
def ark_locate_object(
    image: str,
    object_desc: str,
) -> dict:
    """Locate an object in an image, returning its bounding box.

    Gives the caller spatial coordinates so edits can target a region precisely.

    Args:
        image: Local path or URL of the image
        object_desc: What to find, e.g. "the girl's head" or "the coffee cup"
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not image or not image.strip():
        return _err("invalid_argument", "image is required.")
    if not object_desc or not object_desc.strip():
        return _err("invalid_argument", "object_desc must not be empty.")
    try:
        data, _ = load_input_image(image)
        uri = _downscale_uri(data)
    except Exception as e:
        return _err("load", f"failed to load image {image}: {e}")

    instruction = (
        f"Locate '{object_desc.strip()}' in the image. Reply with ONLY a JSON array "
        "[x1, y1, x2, y2] (0-1000 normalized, top-left origin, x1<x2, y1<y2). "
        "If not present, reply with null."
    )
    raw = None
    try:
        raw = _chat_with_image(instruction, uri, max_tokens=100, timeout=300)
    except Exception as e:
        try:  # one retry — vision service occasionally times out
            raw = _chat_with_image(instruction, uri, max_tokens=100, timeout=300)
        except Exception as e2:
            return _err("api", f"locate chat failed: {e2}")
    bbox = _parse_bbox(raw)
    if bbox is None:
        return _ok(found=False, bbox=None, raw=(raw or "").strip()[:100])
    return _ok(found=True, bbox=bbox, raw=None)


# ---------- 空间操作层：region/mask 编辑 ----------

def _region_to_mask(img_size: tuple, region: str) -> Optional[bytes]:
    """Convert a '[x1,y1,x2,y2]' (0-1000) region into a white-on-black mask PNG bytes
    (white = edit area, black = preserve). Returns None if the region is invalid."""
    bbox = _parse_bbox(region)
    if not bbox:
        return None
    w, h = img_size
    x1, y1, x2, y2 = bbox
    # clamp to image
    px1 = max(0, int(x1 / 1000 * w)); py1 = max(0, int(y1 / 1000 * h))
    px2 = min(w, int(x2 / 1000 * w)); py2 = min(h, int(y2 / 1000 * h))
    if px2 <= px1 or py2 <= py1:
        return None
    mask = PILImage.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.rectangle([px1, py1, px2, py2], fill=255)
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def _mask_to_uri(mask_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(mask_bytes).decode()}"


@mcp.tool()
def ark_generate_image(
    prompt: str,
    image: Optional[str] = None,
    size: str = "2K",
    watermark: bool = False,
    response_format: str = "url"
) -> ImageResult:
    """Generate images using Doubao-Seedream image generation model on 火山方舟.

    Args:
        prompt: Text description for image generation
        image: Optional URL(s) of reference image(s) or local file path(s). Comma-separated for multiple.
        size: Image size. Square presets "1K"/"2K"/"4K" or explicit "WIDTHxHEIGHT"
              (each 512-4096, e.g. "2048x1366" landscape, "1366x2048" portrait)
        watermark: Whether to add watermark
        response_format: Response format. Either "url" (returns downloadable URLs) or "b64_json" (base64-encoded images)
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not prompt or not prompt.strip():
        return _err("invalid_argument", "prompt must not be empty.")
    if not is_valid_size(size):
        return _err("invalid_argument",
                    f"invalid size '{size}'. Valid options: square presets 1K/2K/4K "
                    f"or WIDTHxHEIGHT with each side 512-4096.")
    if response_format not in VALID_RESPONSE_FORMATS:
        return _err("invalid_argument", "invalid response_format. Valid options: url, b64_json")

    output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    payload = {
        "model": ARK_MODEL,
        "prompt": prompt.strip(),
        "response_format": response_format,
        "size": size,
        "watermark": watermark
    }

    notes = []
    if image:
        sources = [s.strip() for s in image.split(",")]
        processed = []
        for src in sources:
            try:
                data, label = load_input_image(src)
                short_label = label[:60] + "..." if len(label) > 60 else label
                compressed, was_compressed = maybe_compress(data)
                if was_compressed:
                    notes.append(f"Input modified (orientation fixed or dim >= {MAX_INPUT_DIM}px resized): {short_label}")
                processed.append(to_base64_data_uri(compressed))
            except Exception as e:
                notes.append(f"Failed to load input: {src} - {e}")
                processed.append(src)
        if len(processed) == 1:
            payload["image"] = processed[0]
        else:
            payload["image"] = processed

    logger.info("Generating image: size=%s response_format=%s model=%s", size, response_format, ARK_MODEL)
    return _generate_images(payload, output_dir, notes)


def _generate_images(payload: dict, output_dir: str, notes: Optional[list] = None,
                     extra_info: Optional[dict] = None) -> dict:
    """Call the Seedream generations API, save results, and return a structured payload."""
    notes = notes or []
    try:
        resp = _post_with_retry(
            f"{ARK_API_BASE}/images/generations",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"},
            json_body=payload,
            timeout=ARK_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.RequestException as e:
        detail = e.response.text if e.response is not None else str(e)
        logger.error("Image generation API request failed: %s", detail)
        return _err("api", detail)

    data_list = result.get("data", [])
    if not data_list:
        return _ok(files=[], usage=result.get("usage"), notes=notes or None, raw=result)

    local_paths = []
    urls = []
    errors = []
    for i, item in enumerate(data_list):
        url = item.get("url", "")
        if url:
            try:
                path = download_image(url, output_dir)
                local_paths.append(path)
            except Exception as e:
                errors.append(f"Image {i} download failed: {e}")
                urls.append(url)  # keep the URL so the image is still reachable
            continue
        b64 = item.get("b64_json", "")
        if b64:
            try:
                path = _save_b64_data(b64, output_dir)
                local_paths.append(path)
            except Exception as e:
                errors.append(f"Image {i} base64 decode failed: {e}")
                urls.append(f"data:{b64[:80]}...")  # base64 not recoverable to a file; keep a hint
            continue
        errors.append(f"Image {i} has neither url nor b64_json")

    info = {
        "model": result.get("model"),
        "created": result.get("created"),
        "files": local_paths,
        "urls": urls if urls else None,
        "usage": result.get("usage"),
        "notes": notes if notes else None,
    }
    if extra_info:
        info.update(extra_info)
    if errors:
        info["errors"] = errors
    return _ok(**info)


@mcp.tool()
def ark_edit_image(
    intent: str,
    image: str,
    size: str = "2K",
    watermark: bool = False,
    scene_profile: Optional[str] = None,
    region: Optional[str] = None,
    mask: Optional[str] = None,
    context_images: Optional[str] = None,
) -> ImageResult:
    """Edit a reference image via a two-step pipeline: the multimodal model (ARK_VISION_MODEL)
    rewrites your edit intent into a detailed, faithful English prompt based on the reference
    image, then Doubao-Seedream generates the edited image.

    Args:
        intent: Your edit intent in natural language (e.g. "slightly enlarge the bust of the girl
                on the left; keep her clothing structure and colors unchanged")
        image: Local path or URL of the reference image
        size: Image size. Square presets "1K"/"2K"/"4K" or "WIDTHxHEIGHT" (each side 512-4096)
        watermark: Whether to add watermark
        scene_profile: Optional scene profile text from ark_scene_profile for the same
                scene/person; injected into prompt refinement to keep consistency
        region: Optional '[x1,y1,x2,y2]' (0-1000 normalized) to restrict the edit to that
                region via a mask; everything outside is preserved (white = edit area)
        mask: Optional path/URL to a mask image (white = edit area, black = preserve);
                overrides `region`
        context_images: Optional comma-separated additional same-scene photos, used as
                visual memory for the prompt refinement (consistency across the scene)
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not intent or not intent.strip():
        return _err("invalid_argument", "intent must not be empty.")
    if not image or not image.strip():
        return _err("invalid_argument", "image is required.")
    if not is_valid_size(size):
        return _err("invalid_argument",
                    f"invalid size '{size}'. Valid options: square presets 1K/2K/4K "
                    f"or WIDTHxHEIGHT with each side 512-4096.")

    output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    notes = []
    try:
        data, label = load_input_image(image)
        short_label = label[:60] + "..." if len(label) > 60 else label
        compressed, was_compressed = maybe_compress(data)
        if was_compressed:
            notes.append(f"Input modified (orientation fixed or dim >= {MAX_INPUT_DIM}px resized): {short_label}")
        processed = to_base64_data_uri(compressed)
        pil_img = PILImage.open(io.BytesIO(compressed))
        img_size = pil_img.size
    except Exception as e:
        return _err("load", f"failed to load reference image {image}: {e}")

    # 空间操作：region 或 mask -> seedream mask 参数
    payload_mask = None
    if mask and mask.strip():
        try:
            mask_data, _ = load_input_image(mask)
            payload_mask = _mask_to_uri(mask_data)
            notes.append(f"Using mask from: {mask}")
        except Exception as e:
            return _err("load", f"failed to load mask image {mask}: {e}")
    elif region and region.strip():
        mask_bytes = _region_to_mask(img_size, region)
        if mask_bytes is None:
            return _err("invalid_argument",
                        f"invalid region '{region}'. Expected [x1,y1,x2,y2] (0-1000).")
        payload_mask = _mask_to_uri(mask_bytes)
        notes.append(f"Using region mask: {region}")

    # 视觉记忆：context_images 并入 prompt 重构
    context_uris = None
    if context_images and context_images.strip():
        context_uris = []
        for src in context_images.split(","):
            try:
                cdata, _ = load_input_image(src)
                context_uris.append(_downscale_uri(cdata))
            except Exception as e:
                notes.append(f"Context image failed to load, skipped: {src} - {e}")

    refined_prompt = None
    try:
        logger.info("Refining edit prompt with vision model %s", ARK_VISION_MODEL)
        if context_uris:
            refined_prompt = refine_edit_prompt_with_context(
                compressed, intent, scene_profile, context_uris)
        else:
            refined_prompt = refine_edit_prompt(compressed, intent, scene_profile)
    except Exception as e:
        notes.append(f"Prompt refinement failed, using raw intent instead: {e}")

    payload = {
        "model": ARK_MODEL,
        "prompt": refined_prompt if refined_prompt else intent.strip(),
        "image": processed,
        "response_format": "url",
        "size": size,
        "watermark": watermark,
    }
    if payload_mask:
        payload["mask"] = payload_mask
    extra = {"refined_prompt": refined_prompt} if refined_prompt else None
    logger.info("Generating edited image: size=%s mask=%s model=%s", size, bool(payload_mask), ARK_MODEL)
    return _generate_images(payload, output_dir, notes, extra)


@mcp.tool()
def ark_revise_image(
    revision: str,
    image: str,
    reference: Optional[str] = None,
    size: str = "2K",
    watermark: bool = False,
    scene_profile: Optional[str] = None,
) -> ImageResult:
    """Revise a previously edited image based on user feedback.

    The multimodal model rewrites a revision prompt from the feedback: it regenerates the image
    from the current result, fixing ONLY the issues listed in the feedback and keeping everything
    else identical to the current result (person, pose, outfit, background, lighting, composition).

    Args:
        revision: Your review feedback on the previous result, e.g. "the belly button looks odd;
                  the white top is smooth over the chest and only ribbed below the bust"
        image: Local path or URL of the previous edited result to revise
        reference: Optional original reference photo, used to confirm original appearance details
        size: Image size. Square presets "1K"/"2K"/"4K" or "WIDTHxHEIGHT" (each side 512-4096)
        watermark: Whether to add watermark
        scene_profile: Optional scene profile text from ark_scene_profile
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not revision or not revision.strip():
        return _err("invalid_argument", "revision must not be empty.")
    if not image or not image.strip():
        return _err("invalid_argument", "image is required.")
    if not is_valid_size(size):
        return _err("invalid_argument",
                    f"invalid size '{size}'. Valid options: square presets 1K/2K/4K "
                    f"or WIDTHxHEIGHT with each side 512-4096.")

    output_dir = os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    notes = []
    try:
        data, label = load_input_image(image)
        short_label = label[:60] + "..." if len(label) > 60 else label
        compressed, was_compressed = maybe_compress(data)
        if was_compressed:
            notes.append(f"Result modified (orientation fixed or dim >= {MAX_INPUT_DIM}px resized): {short_label}")
        processed = to_base64_data_uri(compressed)
    except Exception as e:
        return _err("load", f"failed to load result image {image}: {e}")

    reference_data = None
    if reference and reference.strip():
        try:
            ref_data, _ = load_input_image(reference)
            reference_data, _ = maybe_compress(ref_data)
        except Exception as e:
            notes.append(f"Reference image load failed, ignored: {e}")

    refined_prompt = None
    try:
        logger.info("Revising edit with vision model %s", ARK_VISION_MODEL)
        refined_prompt = revise_edit_prompt(compressed, revision, reference_data, scene_profile)
    except Exception as e:
        notes.append(f"Revision prompt refinement failed, using raw revision instead: {e}")

    payload = {
        "model": ARK_MODEL,
        "prompt": refined_prompt if refined_prompt else revision.strip(),
        "image": processed,
        "response_format": "url",
        "size": size,
        "watermark": watermark,
    }
    notes.append(f"Revision of previous result: {short_label}")
    extra = {"refined_prompt": refined_prompt} if refined_prompt else None
    logger.info("Generating revised image: size=%s model=%s", size, ARK_MODEL)
    return _generate_images(payload, output_dir, notes, extra)


def _capture_screen_region(region: Optional[str] = None) -> Optional[PILImage.Image]:
    """Capture the screen (whole or a region) using PIL.ImageGrab.

    region is '[x1,y1,x2,y2]' in 0-1000 normalized screen coords.
    Returns a PIL Image, or None on platforms without ImageGrab.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    if not region:
        return ImageGrab.grab()
    bbox = _parse_bbox(region)
    if not bbox:
        return None
    sw, sh = ImageGrab.grab().size
    x1, y1, x2, y2 = bbox
    px1 = max(0, int(x1 / 1000 * sw)); py1 = max(0, int(y1 / 1000 * sh))
    px2 = min(sw, int(x2 / 1000 * sw)); py2 = min(sh, int(y2 / 1000 * sh))
    if px2 <= px1 or py2 <= py1:
        return None
    return ImageGrab.grab(bbox=(px1, py1, px2, py2))


@mcp.tool()
def ark_capture_screen(
    region: Optional[str] = None,
    max_dim: int = 2048,
) -> dict:
    """Capture the current screen (or a region) and save it as a PNG.

    Gives the agent the ability to visually inspect the current state of the
    software it is working with — take a screenshot, then feed it to
    ark_analyze_image / ark_locate_object / ark_verify_edit for visual QA.

    Args:
        region: Optional '[x1,y1,x2,y2]' (0-1000 normalized screen coords) to
                capture only part of the screen (e.g. a specific window area)
        max_dim: Downscale the captured image so its longest side is <= this
                (default 2048), keeping screenshots manageable
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    img = _capture_screen_region(region)
    if img is None:
        return _err("api", "screen capture not supported on this platform (needs PIL.ImageGrab, "
                           "e.g. Windows/macOS).")
    try:
        if max_dim and max(img.size) > max_dim:
            ratio = max_dim / max(img.size)
            img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), PILImage.LANCZOS)
        output_dir = os.getcwd()
        os.makedirs(output_dir, exist_ok=True)
        ts = int(time.time())
        token = uuid.uuid4().hex[:6]
        filepath = os.path.join(output_dir, f"ark_screen_{ts}_{token}.png")
        img.save(filepath, format="PNG")
    except Exception as e:
        return _err("load", f"failed to save screenshot: {e}")
    logger.info("Screen captured: %s (%s)", filepath, img.size)
    return _ok(file=filepath, size=list(img.size))


def _parse_json_obj(text: str) -> Optional[dict]:
    """Best-effort JSON object parse from model output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


@mcp.tool()
def ark_verify_edit(
    original: str,
    edited: str,
    intent: str,
) -> dict:
    """Verify an edited image against the original and the edit intent.

    Closes the perception->generation loop: the vision model compares the two images
    and reports whether the requested change was applied, whether anything unintended
    changed, and whether there are artifacts. The caller can decide whether to retry.

    Args:
        original: Local path or URL of the original reference image
        edited: Local path or URL of the edited result image
        intent: The edit intent that was requested
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not original or not edited or not intent:
        return _err("invalid_argument", "original, edited and intent are required.")
    try:
        orig_data, _ = load_input_image(original)
        edit_data, _ = load_input_image(edited)
        orig_uri = _downscale_uri(orig_data)
        edit_uri = _downscale_uri(edit_data)
    except Exception as e:
        return _err("load", f"failed to load images: {e}")

    instruction = (
        "You are an image-editing QA reviewer. The FIRST image is the original reference photo; "
        "the SECOND is the edited result. The edit intent was:\n"
        f"[Intent] {intent}\n\n"
        "Evaluate: 1) Was the requested modification applied, at the right extent? "
        "2) Did anything unintended change (clothing, other people, background, lighting, composition)? "
        "3) Any visible artifacts or distortion?\n"
        'Reply with JSON ONLY, exactly: {"passed": true/false, '
        '"reasons": ["each failure reason in Chinese; empty list if passed"], '
        '"summary": "one-sentence verdict in Chinese"}'
    )
    try:
        raw = _chat_with_images(instruction, [orig_uri, edit_uri], max_tokens=600)
    except Exception as e:
        return _err("api", f"verify chat failed: {e}")
    result = _parse_json_obj(raw)
    if result is None or result.get("passed") is None:
        return _ok(passed=None, reasons=[], summary=raw.strip()[:300], raw=raw)
    return _ok(passed=result.get("passed"), reasons=result.get("reasons", []),
               summary=result.get("summary", ""), raw=None)


IDENTITY_TEMPLATE_PRESETS = [
    ("正面半身像", "正面朝向镜头，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("四十五度侧面半身像", "四十五度侧面朝向镜头，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("正面微笑半身像", "正面朝向镜头自然微笑，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("四十五度侧面微笑半身像", "四十五度侧面朝向镜头自然微笑，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("正面全身像", "正面朝向镜头全身入画，自然站姿，双臂自然垂放，手臂完整不被切出画面"),
]


MAX_VISION_IMAGES = 5  # vision model is flaky with many images in one request


def _sample_images(items: list, limit: int = MAX_VISION_IMAGES) -> list:
    """Evenly sample at most `limit` items from a list (keeps angle variety)."""
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


@mcp.tool()
def ark_generate_identity_templates(
    images: str,
    count: int = 3,
) -> dict:
    """Generate standardized identity template images from multiple photos of one person.

    Pipeline:
    1) The vision model reads all photos, produces an identity profile (facial
       features, body type, hair, distinctive traits) and picks the best front-facing
       anchor photo.
    2) For each preset (front headshot, three-quarter, side, full body...), the anchor
       photo is the main reference, the other photos are visual memory, and the
       identity profile is the text lock; the template is generated with a NEUTRAL
       background/attire so clothing/background leakage is avoided at the source.
    Templates are meant for human review — pick the best 2-4 and reuse them as the
    reference for ark_edit_image to generate portraits.

    Args:
        images: Comma-separated local paths or URLs of the person's photos
        count: How many templates to generate (1-5), from the preset list in order
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not images or not images.strip():
        return _err("invalid_argument", "images must not be empty.")
    if not (1 <= count <= len(IDENTITY_TEMPLATE_PRESETS)):
        return _err("invalid_argument",
                    f"count must be 1-{len(IDENTITY_TEMPLATE_PRESETS)}.")

    sources = [s.strip() for s in images.split(",")]
    loaded = []
    problems = []
    for src in sources:
        try:
            data, label = load_input_image(src)
            loaded.append((data, os.path.basename(label), label))
        except Exception as e:
            problems.append(f"{src}: {e}")
    if not loaded:
        return _err("load", f"no images could be loaded: {problems}")
    if problems:
        logger.warning("ark_generate_identity_templates: some images failed: %s", problems)

    # Vision model is slow with many images — process in batches of 5 (a size known
    # to work) and merge the per-batch descriptions into one identity profile.
    IDENTITY_BATCH = 5
    profile_parts = []
    anchor_candidates = []  # (filename, score-ish order) from each batch
    all_uris = [_downscale_uri(d) for d, _, _ in loaded]
    for start in range(0, len(all_uris), IDENTITY_BATCH):
        batch = all_uris[start:start + IDENTITY_BATCH]
        batch_names = [loaded[start + i][1] for i in range(len(batch))]
        instruction = (
            f"下面是同一个人的 {len(batch)} 张照片（文件名：{', '.join(batch_names)}）。\n"
            "请完成两个任务并只输出一个 JSON 对象：\n"
            '{"profile": "...", "anchor": "<文件名>"}\n'
            "1) profile: 用中文描述这张/这些照片中此人的身份特征：脸型、眼型/眼距、眉形、鼻型、"
            "嘴唇、下颌、发型发色、肤色、体态/肩型，以及任何标志性特征（痣、酒窝等）。"
            "如果本批有多张照片，综合描述共有的身份特征，并补充各张独有的细节。\n"
            "2) anchor: 从本批照片中选出最适合作为身份锚点的一张——正面朝向镜头、面部清晰、"
            "光线好、表情自然、无遮挡。若本批没有合适的（如都是侧面/远景），返回 null。"
            "返回该照片的文件名（必须来自上述列表）。\n"
        )
        try:
            raw = _chat_with_images(instruction, batch, max_tokens=900)
        except Exception as e:
            logger.warning("Identity batch %d chat failed: %s", start // IDENTITY_BATCH, e)
            continue
        parsed = _parse_json_obj(raw) or {}
        part = str(parsed.get("profile") or raw).strip()
        if part:
            profile_parts.append(part)
        anch = str(parsed.get("anchor") or "").strip()
        if anch and any(anch.lower() in n.lower() or n.lower() in anch.lower() for n in batch_names):
            anchor_candidates.append(anch)

    if not profile_parts:
        return _err("api", "identity profile could not be produced from any batch")
    profile = "\n".join(profile_parts)[:2500]

    # Anchor: prefer candidates reported by batches; fall back to first photo.
    anchor_path = loaded[0][2]
    anchor_name = ""
    for cand in anchor_candidates:
        for i, (_, name, path) in enumerate(loaded):
            if cand.lower() in name.lower() or name.lower() in cand.lower():
                anchor_path = path
                anchor_name = name
                break
        if anchor_name:
            break
    context_paths = [loaded[i][2] for i in range(len(loaded))
                     if loaded[i][2] != anchor_path]

    templates = []
    # 输出目录：优先存到输入图所在目录，方便把模板和原图放一起
    output_dir = os.path.dirname(os.path.abspath(anchor_path)) or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)
    prev_cwd = os.getcwd()
    os.chdir(output_dir)  # ark_edit_image saves to cwd; run inside the input folder

    # 模板组合：默认生成 正面 + 四十五度侧面 各一张（上半身到腰部、双臂完整）
    template_specs = [
        ("正面半身像", IDENTITY_TEMPLATE_PRESETS[0][1]),
        ("四十五度侧面半身像", IDENTITY_TEMPLATE_PRESETS[1][1]),
    ]
    if count > 2:
        template_specs += [(d, p) for d, p in IDENTITY_TEMPLATE_PRESETS[2:2 + (count - 2)]]

    # 身份画像存为 md，放在同目录，供后续生图参考
    profile_md = f"# Identity Profile\n\n{profile}\n"
    md_path = os.path.join(output_dir, "identity_profile.md")
    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(profile_md)
        logger.info("Identity profile saved to %s", md_path)
    except Exception as e:
        logger.warning("Failed to write identity profile md: %s", e)
        md_path = None

    templates = []
    try:
        for desc, pose in template_specs:
            intent = (
                f"生成一张{desc}：{pose}。\n"
                "要求：整体呈自然生活照质感，不是证件照——背景为柔和的自然室内/户外中性环境"
                "（如素雅的米白色墙面、虚化的自然光，无任何图案、文字、logo）；"
                "穿着为简约的自然色系便装（如浅色基础款，无图案无logo，不要正式西装或制服）；"
                "表情自然放松，光线柔和自然，符合日常真实照片的质感；"
                "构图必须把人物完整上半身（到腰部）纳入画面，双臂完整可见、左右手臂都不被裁切；"
                "面部必须是参考人物的真实身份（严格按身份画像的五官特征）；"
                "忽略参考图中原有的服装、背景、光线、色调，只保留人物的面部与体态身份特征。"
            )
            out = ark_edit_image(
                intent=intent,
                image=anchor_path,
                size="1024x1536",
                scene_profile=profile,
                context_images=",".join(context_paths) if context_paths else None,
            )
            if out.get("ok"):
                files = out.get("files") or []
                tpl_file = files[0] if files else None
                templates.append({"preset": desc, "file": tpl_file,
                                  "refined_prompt": out.get("refined_prompt")})
            else:
                templates.append({"preset": desc, "error": out.get("error")})
    finally:
        os.chdir(prev_cwd)

    return _ok(identity_profile=profile, identity_profile_md=md_path,
               anchor=os.path.basename(anchor_path),
               output_dir=output_dir, templates=templates, total=len(templates))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
