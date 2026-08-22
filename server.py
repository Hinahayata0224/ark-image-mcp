import base64
import io
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

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
    from PIL import Image as PILImage, ImageOps
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
    return {"ok": False, "error_type": error_type, "error": message}


def _ok(**fields) -> dict:
    """Unified success payload: {"ok": true, **fields}."""
    return {"ok": True, **fields}


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
    """Normalize EXIF orientation, then resize to 1/2 if dimensions >= MAX_INPUT_DIM.

    Returns (img_data, compressed) where compressed indicates whether a resize happened.
    """
    if not HAS_PIL:
        return img_data, False
    try:
        img = PILImage.open(io.BytesIO(img_data))
        img = ImageOps.exif_transpose(img)  # honor EXIF orientation (e.g. phone photos)
        w, h = img.size
        if w < MAX_INPUT_DIM and h < MAX_INPUT_DIM:
            buf = io.BytesIO()
            img.save(buf, format=img.format or "JPEG")
            return buf.getvalue(), False
        new_w = w // 2
        new_h = h // 2
        img = img.resize((new_w, new_h), PILImage.LANCZOS)
        buf = io.BytesIO()
        fmt = img.format or "PNG"
        img.save(buf, format=fmt)
        return buf.getvalue(), True
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


def _chat_with_image(user_text: str, image_data_uri: str, max_tokens: int = 1500) -> str:
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
    resp = requests.post(f"{ARK_API_BASE}/chat/completions", headers=headers, json=body, timeout=180)
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


def _chat_with_images(user_text: str, image_uris: list, max_tokens: int = 2000) -> str:
    """Call the multimodal chat model (ARK_VISION_MODEL) with text and multiple images."""
    content = [{"type": "image_url", "image_url": {"url": u}} for u in image_uris]
    content.append({"type": "text", "text": user_text})
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ARK_API_KEY}"}
    body = {
        "model": ARK_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    resp = requests.post(f"{ARK_API_BASE}/chat/completions", headers=headers, json=body,
                         timeout=(30, 300))
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


@mcp.tool()
def ark_scene_profile(images: str, focus: Optional[str] = None) -> dict:
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


@mcp.tool()
def ark_generate_image(
    prompt: str,
    image: Optional[str] = None,
    size: str = "2K",
    watermark: bool = False,
    response_format: str = "url"
) -> dict:
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
                    notes.append(f"Input compressed (dim >= {MAX_INPUT_DIM}px -> 1/2): {short_label}")
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
    errors = []
    for i, item in enumerate(data_list):
        url = item.get("url", "")
        if url:
            try:
                path = download_image(url, output_dir)
                local_paths.append(path)
            except Exception as e:
                errors.append(f"Image {i} download failed: {e}")
                local_paths.append(url)
            continue
        b64 = item.get("b64_json", "")
        if b64:
            try:
                path = _save_b64_data(b64, output_dir)
                local_paths.append(path)
            except Exception as e:
                errors.append(f"Image {i} base64 decode failed: {e}")
            continue
        errors.append(f"Image {i} has neither url nor b64_json")

    info = {
        "model": result.get("model"),
        "created": result.get("created"),
        "files": local_paths,
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
) -> dict:
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
            notes.append(f"Input compressed (dim >= {MAX_INPUT_DIM}px -> 1/2): {short_label}")
        processed = to_base64_data_uri(compressed)
    except Exception as e:
        return _err("load", f"failed to load reference image {image}: {e}")

    refined_prompt = None
    try:
        logger.info("Refining edit prompt with vision model %s", ARK_VISION_MODEL)
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
    extra = {"refined_prompt": refined_prompt} if refined_prompt else None
    logger.info("Generating edited image: size=%s model=%s", size, ARK_MODEL)
    return _generate_images(payload, output_dir, notes, extra)


@mcp.tool()
def ark_revise_image(
    revision: str,
    image: str,
    reference: Optional[str] = None,
    size: str = "2K",
    watermark: bool = False,
    scene_profile: Optional[str] = None,
) -> dict:
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
            notes.append(f"Result compressed (dim >= {MAX_INPUT_DIM}px -> 1/2): {short_label}")
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
