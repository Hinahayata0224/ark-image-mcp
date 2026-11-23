import base64
import io
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import uuid
from typing import Optional

from pydantic import BaseModel, Field

import requests
from mcp.server.fastmcp import FastMCP

from uploader import describe_config, upload_paths

# Transport settings. stdio is the default so existing desktop clients (ZCode,
# Claude Desktop, ...) keep working unchanged; set ARK_TRANSPORT=http to serve
# over streamable HTTP for remote clients such as Chatbox on a phone.
# These are read before FastMCP is constructed because host/port are ctor args.
ARK_TRANSPORT = os.environ.get("ARK_TRANSPORT", "stdio").strip().lower()
ARK_HOST = os.environ.get("ARK_HOST", "127.0.0.1").strip()
ARK_PORT = int(os.environ.get("ARK_PORT", "8765"))
ARK_HTTP_PATH = os.environ.get("ARK_HTTP_PATH", "/mcp").strip() or "/mcp"
# Bearer token required by the HTTP transport. Without it the server refuses to
# start in http mode: an open endpoint would let anyone spend your Ark credits.
ARK_MCP_TOKEN = os.environ.get("ARK_MCP_TOKEN", "").strip()
# Where images pushed to POST /upload are written. Defaults to a temp dir.
ARK_INPUT_DIR = os.environ.get("ARK_INPUT_DIR", "").strip()
# Public base URL of this server, used only to word instructions for remote
# clients (e.g. https://mcp.example.com). Optional.
ARK_PUBLIC_BASE_URL = os.environ.get("ARK_PUBLIC_BASE_URL", "").strip().rstrip("/")
# Hard cap for /upload bodies.
ARK_MAX_UPLOAD_MB = int(os.environ.get("ARK_MAX_UPLOAD_MB", "25"))
# Separate credential for POST /upload, so remote clients (whose model cannot
# see the MCP session's headers) can be told a token without handing over the
# main one. Its blast radius is limited to writing image files into a temp dir;
# it cannot call tools or spend Ark credits. Falls back to ARK_MCP_TOKEN.
ARK_UPLOAD_TOKEN = os.environ.get("ARK_UPLOAD_TOKEN", "").strip()


def upload_hint(source: str = "<local file>") -> str:
    """Return a ready-to-run upload command for the current configuration.

    Embedded in the MCP instructions and in load errors, so a client that hits
    an unreadable path is told exactly what to run instead of having to
    discover the /upload endpoint on its own.
    """
    base = ARK_PUBLIC_BASE_URL
    if not base:
        base = (f"http://127.0.0.1:{ARK_PORT}" if ARK_HOST in ("127.0.0.1", "localhost")
                else f"http://{ARK_HOST}:{ARK_PORT}")
    token = ARK_UPLOAD_TOKEN or ARK_MCP_TOKEN or "<upload-token>"
    return (f"curl -sS -X POST '{base}/upload' "
            f"-H 'Authorization: Bearer {token}' "
            f"--data-binary '@{source}'")


def upload_page_url() -> str:
    """The browser upload page URL, with the upload token embedded for convenience."""
    base = ARK_PUBLIC_BASE_URL
    if not base:
        base = (f"http://127.0.0.1:{ARK_PORT}" if ARK_HOST in ("127.0.0.1", "localhost")
                else f"http://{ARK_HOST}:{ARK_PORT}")
    token = ARK_UPLOAD_TOKEN or ARK_MCP_TOKEN
    return f"{base}/upload?token={urllib.parse.quote(token)}" if token else f"{base}/upload"

mcp = FastMCP(
    "ark-image-gen",
    instructions=(
        "MCP server for 火山方舟 (Volcano Ark) Doubao-Seedream image generation and editing.\n\n"
        "*** READ THIS FIRST IF THE USER ATTACHED A PHOTO ***\n"
        "This server may run on a DIFFERENT machine than your client. A file the user attached in "
        "the chat (e.g. 'input/image-abc.jpg', or any workspace-relative path) does NOT exist here. "
        "Do not guess server paths, do not search the server's disk, and do not try to rewrite the "
        "relative path. In order of preference:\n"
        "  0. BEST FOR PHONES: tell the user to open <UPLOAD_PAGE> in a phone browser and pick the "
        "photo there. The browser uploads it directly — no base64 in the conversation, no shell "
        "needed, any file size. Afterwards call ark_recent_uploads() and use files[0].path. "
        "Suggest this first whenever the image is large or your sandbox cannot run shell commands.\n"
        "  1. If your client can give you a PUBLIC http(s) URL for that image (a download link that "
        "opens in a normal browser), pass that URL as the `image` argument. Zero extra cost.\n"
        "  2. Otherwise call ark_upload_image and pass the returned `path` as `image`. This travels over "
        "the MCP connection, so it still works when your sandbox has no shell networking.\n"
        "     * If the base64 is too big to emit in ONE tool call (roughly >20 KB of base64, which is "
        "any normal photo), SLICE IT: call ark_upload_image repeatedly with the same upload_id, "
        "incrementing index, and total=N. Split the base64 STRING on a multiple-of-4 boundary and keep "
        "the slices in order. Each reply is {\"ok\":true,\"pending\":true,\"received\":n}; the last one "
        "returns the path. Do not give up because a single call is too large — chunking is expected.\n"
        "  3. Only if you have a shell WITH internet access, curl the bytes: <UPLOAD_COMMAND>\n"
        "Do NOT paste a large base64 blob straight into ark_edit_image's arguments: use "
        "ark_upload_image once and reuse the short path. If the file is large, downscale it in your "
        "sandbox before base64-encoding (e.g. to ~900px longest side) — that cuts the payload by "
        "an order of magnitude and does not hurt editing quality.\n"
        "ark_upload_image returns {\"ok\": true, \"path\": ...}; if it fails, report the error to the "
        "user instead of retrying other transports.\n\n"
        "*** SLOW CALLS AND PROXY TIMEOUTS ***\n"
        "A proxied MCP connection is cut after roughly 100 seconds of silence, while editing "
        "(vision rewrite + generation) routinely needs 2-3 minutes. So for ark_edit_image, "
        "ark_revise_image, ark_portrait_from_ref and ark_generate_identity_templates, prefer:\n"
        "    ark_start_job(tool='ark_edit_image', arguments='{\"intent\":..., \"image\":...}')\n"
        "  -> returns {\"job_id\": ...} immediately; then call ark_job_status(job_id, wait_seconds=40) "
        "and repeat until status is 'ok' or 'error'. The `result` field is the tool's normal output. "
        "ark_generate_image alone is usually fast enough to call directly.\n"
        "If a direct call fails with a connection error or 524, that is this timeout, not a real "
        "failure — retry the same work through ark_start_job instead of telling the user it broke.\n\n"
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
        "- image inputs: local path, http(s) URL, `data:<mime>;base64,<payload>`, or a `b64:<payload>` "
        "string; comma-separate multiple (mainly for ark_scene_profile). "
        "Images with EXIF rotation are auto-corrected; images >= 5000px are auto-downscaled.\n"
        "- IMAGES THAT ONLY EXIST ON THE CLIENT: when this server runs on a different machine than "
        "the client, a file the client can see (a photo the user attached in a phone app, a file in "
        "a sandbox workspace) is NOT readable here, and relative paths will fail. Push the bytes to "
        "the server first:\n"
        "    curl -sS -X POST '<PUBLIC_BASE_URL>/upload' -H 'Authorization: Bearer <upload-token>' "
        "--data-binary @<local file>\n"
        "  This upload token only authorizes writing image files to the server; it is not the MCP "
        "session credential. If the response is 401, the user must supply the upload token.\n"
        "  The JSON reply contains an absolute `path` on the server — pass that to the editing tools. "
        "Small images (a few hundred KB) can instead be inlined as a data: or b64: payload. Never "
        "guesstimate a server path from a client-side relative path.\n"
        "- Output images are saved to the caller's current working directory as ark_img_*.jpg; the report "
        "includes files, usage, notes, and refined_prompt (the actual prompt the vision model wrote - "
        "review it to verify intent fidelity).\n"
        "- If image hosting is configured, the report also carries share_urls (public links) and a "
        "hosting block. Surface share_urls verbatim when the user needs to view an image from another "
        "device or chat app, since local file paths do not open on a phone.\n"
        "- SHOWING AN IMAGE: always prefer MARKDOWN over MCP image blocks. Call "
        "ark_show_image(image=<local path from a previous result>) with mode left at its default "
        "'markdown', then repeat the returned `markdown` field verbatim in your reply — that is what "
        "makes the picture render inside the chat. Do this by default whenever you generate or edit "
        "an image, without waiting to be asked. \n"
        "  * NEVER use mode='block' unless the user explicitly says their client renders MCP images. "
        "Chatbox (including the mobile app) does not: it prints the raw base64 into the conversation, "
        "which costs tens of thousands of tokens and shows no picture.\n"
        "  * If ark_show_image returns an error (e.g. no image host configured), fall back to giving "
        "the share_urls link as plain text.\n"
        "- ALL tools return a JSON object: {\"ok\": true/false, ...}. On success the object carries the "
        "result fields (files, usage, notes, refined_prompt, ...); on failure it carries "
        "{\"ok\": false, \"error_type\": \"config|invalid_argument|load|api\", \"error\": \"message\"}. "
        "ark_scene_profile on success returns {\"ok\": true, \"profile\": \"...\", \"images\": N}."
    ).replace(
        "<UPLOAD_COMMAND>", upload_hint(),
    ).replace(
        "<UPLOAD_PAGE>", upload_page_url(),
    ).replace(
        "<PUBLIC_BASE_URL>",
        ARK_PUBLIC_BASE_URL or (f"http://{ARK_HOST}:{ARK_PORT}" if ARK_HOST != "0.0.0.0"
                               else "http://<this-server>"),
    ).replace(
        "<upload-token>",
        ARK_UPLOAD_TOKEN or ARK_MCP_TOKEN or "<upload-token>",
    ),
    # HTTP transport settings; ignored in stdio mode. stateless_http keeps the
    # server usable behind a plain load balancer / tunnel without session
    # affinity, which is what a phone client needs.
    host=ARK_HOST,
    port=ARK_PORT,
    streamable_http_path=ARK_HTTP_PATH,
    stateless_http=ARK_TRANSPORT in ("http", "streamable-http"),
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
            "profile": None, "images": None, "share_urls": None, "hosting": None}


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
    """Load an image from a local path, http(s) URL, data: URL, or b64: payload.

    Returns (img_data, source_label).

    The data:/b64: forms exist for REMOTE clients: when this server runs on
    another machine than the client, a file the client can see is unreachable
    here. Inlining the bytes is then the only way to pass it along.
    """
    s = (source or "").strip()
    if not s:
        raise ValueError("empty image source")

    if s.startswith("data:"):
        m = re.match(r"^data:([^;,]*)(;base64)?,(.*)$", s, re.S)
        if not m:
            raise ValueError("malformed data URL (expected data:<mime>;base64,<payload>)")
        mime, is_b64, payload = m.group(1), m.group(2), m.group(3)
        data = base64.b64decode(payload, validate=False) if is_b64 \
            else urllib.parse.unquote_to_bytes(payload)
        if not data:
            raise ValueError("data URL decoded to 0 bytes")
        return data, f"data:{mime or 'image/*'} ({len(data)} bytes)"

    if s.startswith("b64:"):
        data = base64.b64decode(s[4:], validate=False)
        if not data:
            raise ValueError("b64: payload decoded to 0 bytes")
        return data, f"base64 payload ({len(data)} bytes)"

    # Reject obviously relative paths early with a message the model can act on,
    # instead of letting requests.get() fail with a confusing URL error.
    try:
        with open(s, "rb") as f:
            img_data = f.read()
        return img_data, os.path.abspath(s)
    except OSError:
        pass
    if not s.startswith(("http://", "https://")):
        raise ValueError(
            f"not a readable local path on this machine and not an http(s) URL: {s!r}. "
            "This server runs on a DIFFERENT machine than your client, so a file your client "
            "can see does not exist here. Do one of these instead:\n"
            "  * pass a public http(s) URL for the image, or\n"
            "  * call ark_upload_image(data=<base64 of the file>) and use the returned `path`, or\n"
            f"  * if you have a shell with internet access: {upload_hint()}\n"
            "Reserve raw data:/b64: payloads for images under ~100 KB; anything larger is far "
            "cheaper to upload once than to inline in every tool call."
        )
    resp = requests.get(s, headers=UA_HEADERS, timeout=120)
    resp.raise_for_status()
    return resp.content, s


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
        _flatten_to_rgb(img).save(buf, format="JPEG", quality=92)
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


def _flatten_to_rgb(img):
    """Convert any PIL image to RGB, compositing transparency onto white.

    JPEG cannot store an alpha channel, so saving an RGBA/P-with-transparency
    image as JPEG raises "cannot write mode RGBA as JPEG". Phones and screenshot
    tools produce such files routinely.
    """
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = PILImage.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _downscale_uri(img_data: bytes, max_dim: int = 1024, quality: int = 85) -> str:
    """Encode image bytes as a downscaled JPEG data URI (for vision-model calls)."""
    img = PILImage.open(io.BytesIO(img_data))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
    img = _flatten_to_rgb(img)
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


def refine_portrait_prompt(identity_data: bytes, style_uri: str,
                           scene_profile: str, extra_intent: Optional[str]) -> str:
    """Rewrite a portrait generation request into a detailed English prompt.

    FIRST image = identity template (keep the face/body); SECOND image = style
    reference (copy pose/outfit/scene/lighting); scene_profile = text lock on the
    face. Roles are explicit so the face is never taken from the style image.
    """
    uri = to_base64_data_uri(identity_data)
    instruction = (
        "You are a professional image-editing prompt engineer. The FIRST image is an IDENTITY "
        "TEMPLATE of a person (their face and body are the identity to preserve). The SECOND image "
        "is a STYLE REFERENCE (its pose, outfit, scene, lighting, and mood are to be copied). "
        "Rewrite the user's request into ONE detailed English prompt for image-to-image generation. "
        "Requirements:\n"
        "1) The person in the output must be EXACTLY the person from the FIRST image — same face, "
        "same facial features, same body. Never use the face or identity of the SECOND (style) image.\n"
        "2) The pose, composition, outfit, scene/background, and lighting of the output follow the "
        "SECOND (style) image — the person is re-posed/re-dressed into that style.\n"
        "3) Describe the outfit/scene from the style image in detail so it is reproduced faithfully.\n"
        "4) Use the identity profile below to lock facial details precisely.\n"
        "5) Output ONLY the prompt text - no explanations, no prefixes, no quotation marks.\n\n"
        f"[Scene profile] {scene_profile}\n"
        f"[Request] {extra_intent or 'generate a natural lifestyle portrait in the style of the second image'}"
    )
    return _chat_with_images(instruction, [uri, style_uri], max_tokens=1500)


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


# ---------- 展示层：让图片在聊天界面里直接显示 ----------

def _to_display_jpeg(img_data: bytes, max_dim: int = 1024, quality: int = 82) -> bytes:
    """Re-encode as a chat-sized JPEG."""
    if not HAS_PIL:
        return img_data
    img = PILImage.open(io.BytesIO(img_data))
    img = ImageOps.exif_transpose(img)
    img = _flatten_to_rgb(img)
    img.thumbnail((max_dim, max_dim), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


@mcp.tool()
def _decode_image_payload(raw: str) -> bytes:
    """Decode a data: URL, b64: payload, or bare base64 (whitespace tolerated)."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("payload is empty")
    if raw.startswith(("data:", "b64:")):
        img_data, _ = load_input_image(raw)
        return img_data
    return base64.b64decode(re.sub(r"\s+", "", raw), validate=False)


def _store_image_bytes(img_data: bytes, filename: str) -> dict:
    """Validate image bytes, write them under the input dir, return the report."""
    if not img_data:
        return _err("invalid_argument", "payload decoded to 0 bytes")

    limit = ARK_MAX_UPLOAD_MB * 1024 * 1024
    if len(img_data) > limit:
        return _err("invalid_argument",
                    f"image is {len(img_data)} bytes, over the {ARK_MAX_UPLOAD_MB} MB limit. "
                    "Downscale it on the client before uploading.")

    ext = ".jpg"
    if HAS_PIL:
        try:
            probe = PILImage.open(io.BytesIO(img_data))
            probe.verify()
            fmt = (probe.format or "JPEG").upper()
            ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                   "GIF": ".gif", "BMP": ".bmp"}.get(fmt, ".jpg")
        except Exception as e:
            return _err("invalid_argument", f"payload is not a decodable image: {e}")

    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(filename))[0])[:40]
    name = f"{(stem or 'upload')}_{uuid.uuid4().hex[:12]}{ext}"
    path = os.path.join(_input_dir(), name)
    try:
        with open(path, "wb") as fh:
            fh.write(img_data)
    except OSError as e:
        return _err("load", f"could not write upload: {e}")

    logger.info("Received MCP upload: %s (%d bytes)", name, len(img_data))
    return _ok(path=path, bytes=len(img_data), filename=name)


# In-flight chunked uploads: upload_id -> {"chunks": {index: str}, "total": n,
# "filename": str, "updated": epoch}. Bounded below so a client that abandons an
# upload cannot grow this forever.
_PENDING_UPLOADS: dict = {}
_PENDING_TTL = 1800  # seconds
_PENDING_MAX = 32


def _sweep_pending(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for key, entry in list(_PENDING_UPLOADS.items()):
        if now - entry["updated"] > _PENDING_TTL:
            _PENDING_UPLOADS.pop(key, None)
    while len(_PENDING_UPLOADS) > _PENDING_MAX:
        oldest = min(_PENDING_UPLOADS, key=lambda k: _PENDING_UPLOADS[k]["updated"])
        _PENDING_UPLOADS.pop(oldest, None)


@mcp.tool()
def ark_upload_image(data: str, filename: str = "", upload_id: str = "",
                     index: int = 0, total: int = 1) -> dict:
    """Store an image that exists only on the client, so the other tools can use it.

    Remote clients often cannot reach this server's /upload endpoint: a mobile
    sandbox typically has no shell networking even though MCP tool calls work
    fine. This tool carries the bytes over the MCP connection itself, which is
    the only channel that reliably exists on both sides.

    CHUNKED MODE — use this when the image is too large to emit in one call.
    A single tool call cannot carry more than roughly 20-30 KB of base64 on most
    clients, so for a big photo send the SAME base64 string in slices:

        ark_upload_image(data=<slice 0>, upload_id="photo1", index=0, total=3)
        ark_upload_image(data=<slice 1>, upload_id="photo1", index=1, total=3)
        ark_upload_image(data=<slice 2>, upload_id="photo1", index=2, total=3)

    Rules for slicing: split the base64 string (not the raw bytes) at a multiple
    of 4 characters, keep the slices in order, and do not reformat them. Each
    intermediate reply is {"ok": true, "pending": true, "received": n, "total": N};
    the final one returns the usual {"ok": true, "path": ...}. Pick any unique
    upload_id you like and reuse it for every slice.

    Args:
        data: base64 image bytes. A full `data:image/jpeg;base64,...` URL, a
              `b64:` prefix, or bare base64 are all accepted; whitespace and
              newlines are ignored. In chunked mode this is ONE slice.
        filename: optional name hint, used only to choose a friendlier stem.
        upload_id: required when total > 1; groups the slices together.
        index: zero-based slice number.
        total: number of slices; 1 (default) means a single complete payload.

    Returns {"ok": true, "path": "<absolute path on this server>", "bytes": N}.
    Pass that `path` as the `image` argument of any other tool here.
    """
    try:
        total = max(1, int(total or 1))
        index = max(0, int(index or 0))
    except (TypeError, ValueError):
        return _err("invalid_argument", "index and total must be integers")

    # ---- single-shot ----
    if total == 1:
        try:
            img_data = _decode_image_payload(data)
        except Exception as e:
            return _err("invalid_argument", f"could not decode base64 payload: {e}")
        return _store_image_bytes(img_data, filename)

    # ---- chunked ----
    if not upload_id.strip():
        return _err("invalid_argument",
                    "upload_id is required when total > 1; pick a unique id and reuse "
                    "it for every slice of this image")
    if index >= total:
        return _err("invalid_argument", f"index {index} is out of range for total {total}")

    _sweep_pending()
    entry = _PENDING_UPLOADS.setdefault(
        upload_id.strip(),
        {"chunks": {}, "total": total, "filename": filename, "updated": time.time()},
    )
    _sweep_pending()  # the insertion above may have pushed past the cap
    if entry["total"] != total:
        return _err("invalid_argument",
                    f"upload_id {upload_id!r} was started with total={entry['total']}, "
                    f"got total={total}")
    if filename:
        entry["filename"] = filename

    slice_data = re.sub(r"\s+", "", data or "")
    if not slice_data:
        return _err("invalid_argument", f"slice {index} is empty")
    entry["chunks"][index] = slice_data
    entry["updated"] = time.time()

    received = len(entry["chunks"])
    if received < total:
        return _ok(pending=True, upload_id=upload_id.strip(), received=received,
                   total=total, next_index=sorted(set(range(total)) - set(entry["chunks"]))[0],
                   hint=f"Send the remaining slices with the same upload_id; "
                        f"{total - received} left.")

    missing = sorted(set(range(total)) - set(entry["chunks"]))
    if missing:
        return _err("invalid_argument", f"missing slices: {missing}")

    joined = "".join(entry["chunks"][i] for i in range(total))
    _PENDING_UPLOADS.pop(upload_id.strip(), None)
    try:
        img_data = base64.b64decode(joined, validate=False)
    except Exception as e:
        return _err("invalid_argument", f"assembled payload does not decode: {e}")
    return _store_image_bytes(img_data, entry["filename"] or filename)


# ---------- background jobs (for calls that outlive a proxy timeout) ----------

# Tool name -> callable. Editing goes through a vision rewrite plus a generation
# call, which together can exceed the ~100 s a proxied request is allowed to
# stay silent (Cloudflare's free-plan proxy read timeout, not adjustable).
_JOBS: dict = {}
_JOB_TTL = 3600
_JOB_MAX = 32
_SLOW_TOOLS = {
    "ark_generate_image",
    "ark_edit_image",
    "ark_revise_image",
    "ark_portrait_from_ref",
    "ark_generate_identity_templates",
    "ark_scene_profile",
}


def _sweep_jobs(now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    for key, job in list(_JOBS.items()):
        if job["done"] and now - job["updated"] > _JOB_TTL:
            _JOBS.pop(key, None)
    while len(_JOBS) > _JOB_MAX:
        oldest = min(_JOBS, key=lambda k: _JOBS[k]["updated"])
        _JOBS.pop(oldest, None)


@mcp.tool()
def ark_start_job(tool: str, arguments: str = "{}") -> dict:
    """Run a slow tool in the background and return a job id immediately.

    Use this when the call may take longer than ~90 seconds — image editing and
    revision run a vision rewrite plus a generation pass, and a proxied HTTP
    connection is often cut before they finish. Submitting returns at once, so
    the transport never goes quiet; then poll ark_job_status until it is done.

    Args:
        tool: name of the slow tool to run, e.g. "ark_edit_image".
        arguments: JSON object string of that tool's arguments, e.g.
                   '{"intent": "...", "image": "C:/.../in.jpg", "size": "1K"}'.

    Returns {"ok": true, "job_id": "...", "tool": "...", "status": "running"}.
    """
    import threading

    name = (tool or "").strip()
    if name not in _SLOW_TOOLS:
        return _err("invalid_argument",
                    f"{name!r} is not a background-capable tool; "
                    f"expected one of {sorted(_SLOW_TOOLS)}")
    fn = globals().get(name)
    if not callable(fn):
        return _err("config", f"tool {name!r} is not available in this build")

    try:
        kwargs = json.loads(arguments) if arguments.strip() else {}
    except json.JSONDecodeError as e:
        return _err("invalid_argument", f"arguments is not valid JSON: {e}")
    if not isinstance(kwargs, dict):
        return _err("invalid_argument", "arguments must be a JSON object")

    _sweep_jobs()
    job_id = uuid.uuid4().hex[:16]
    job = {"id": job_id, "tool": name, "status": "running", "done": False,
           "result": None, "error": None, "started": time.time(), "updated": time.time()}
    _JOBS[job_id] = job

    def _run():
        try:
            job["result"] = fn(**kwargs)
            job["status"] = "ok"
        except Exception as exc:  # surface the failure instead of losing the thread
            logger.exception("Background job %s (%s) failed", job_id, name)
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["status"] = "error"
        finally:
            job["done"] = True
            job["updated"] = time.time()

    threading.Thread(target=_run, name=f"ark-job-{job_id}", daemon=True).start()
    logger.info("Started background job %s for %s", job_id, name)
    return _ok(job_id=job_id, tool=name, status="running",
               hint="Poll ark_job_status(job_id) every few seconds until status is "
                    "'ok' or 'error'. Keep each poll fast.")


@mcp.tool()
def ark_job_status(job_id: str, wait_seconds: int = 0) -> dict:
    """Check a background job started with ark_start_job.

    Args:
        job_id: the id returned by ark_start_job.
        wait_seconds: how long to block waiting for completion (0-60). Raising it
                      to ~40 means fewer round trips; keep it well under the
                      proxy's timeout.

    Returns {"ok": true, "status": "running"|"ok"|"error", "result": {...}}.
    The `result` field is exactly what the underlying tool would have returned.
    """
    job = _JOBS.get((job_id or "").strip())
    if job is None:
        return _err("invalid_argument",
                    f"unknown job_id {job_id!r}; it may have expired "
                    f"({_JOB_TTL}s after completion)")

    wait = max(0, min(int(wait_seconds or 0), 60))
    deadline = time.time() + wait
    while not job["done"] and time.time() < deadline:
        time.sleep(0.25)

    if not job["done"]:
        return _ok(job_id=job_id, status="running", tool=job["tool"],
                   elapsed=round(time.time() - job["started"], 1),
                   hint="Not finished yet; poll again.")

    _sweep_jobs()
    if job["status"] == "ok":
        return _ok(job_id=job_id, status="ok", tool=job["tool"], result=job["result"],
                   elapsed=round(job["updated"] - job["started"], 1))
    return _ok(job_id=job_id, status="error", tool=job["tool"], error=job["error"],
               elapsed=round(job["updated"] - job["started"], 1))


@mcp.tool()
def ark_recent_uploads(limit: int = 5) -> dict:
    """List the most recent images uploaded to this server, newest first.

    Use this when the user says they just uploaded a photo (for example through
    the web page at /upload) but did not paste a path. Take the first entry's
    `path` and pass it as `image`.

    Returns {"ok": true, "files": [{"path", "filename", "bytes", "uploaded_at"}]}.
    """
    files = _recent_uploads(limit)
    return _ok(files=files, count=len(files),
               hint="Use files[0].path as the `image` argument." if files
               else "No uploads yet. Open the /upload page in a browser to add one.")


@mcp.tool()
def ark_show_image(image: str, max_dim: int = 1024, mode: str = "markdown"):
    """Make a generated image visible in the chat.

    `mode="markdown"` (default) returns a Markdown image line plus the public URL.
    Repeat that line verbatim in your reply and the chat client renders the
    picture inline — this works in Chatbox, Cursor, Claude, and most other
    Markdown-based UIs.

    `mode="block"` returns a native MCP `image` content block instead. Only use
    it for clients known to render MCP images; clients that do not (Chatbox is
    one) will dump the raw base64 into the conversation as text.

    Args:
        image: Local path on the server, or an http(s) URL.
        max_dim: Longest side in pixels for the copy that gets uploaded.
        mode: "markdown" (default) or "block".
    """
    from mcp.types import ImageContent, TextContent

    max_dim = max(256, min(int(max_dim or 1024), 2048))
    mode = (mode or "markdown").strip().lower()
    if mode not in ("markdown", "block"):
        return _err("invalid_argument", "mode must be 'markdown' or 'block'")

    if mode == "markdown":
        # A URL input can be linked directly; only local files need hosting.
        if image.startswith(("http://", "https://")):
            markdown = f"![image]({image})"
            return _ok(markdown=markdown, url=image, source=image[:200],
                       note="Repeat the markdown field verbatim in your reply.")

        try:
            data, label = load_input_image(image)
        except Exception as e:
            return _err("load", f"failed to load image: {e}")

        hosting = describe_config()
        if not hosting["enabled"]:
            return _err("config",
                        "no image host configured, so no public URL is available to "
                        "embed. Set ARK_UPLOAD_PROVIDER (e.g. aliyun) or pass an http(s) "
                        f"URL. The local file is still at: {label[:200]}")

        try:
            payload = _to_display_jpeg(data, max_dim=max_dim)
        except Exception as e:
            return _err("load", f"failed to re-encode image: {e}")

        tmpdir = tempfile.mkdtemp(prefix="ark_show_")
        tmp_path = os.path.join(tmpdir, "show.jpg")
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(payload)
            notes = []
            urls, notes = upload_paths([tmp_path], notes)
        finally:
            try:
                os.remove(tmp_path)
                os.rmdir(tmpdir)
            except OSError:
                pass

        if not urls:
            return _err("api", "upload failed: " + ("; ".join(notes) if notes else "no URL returned"))

        markdown = f"![image]({urls[0]})"
        return _ok(markdown=markdown, url=urls[0], source=label[:200],
                   bytes=len(payload),
                   note="Repeat the markdown field verbatim in your reply so the "
                        "client renders the picture.")

    # mode == "block"
    try:
        data, label = load_input_image(image)
    except Exception as e:
        return _err("load", f"failed to load image: {e}")

    try:
        payload = _to_display_jpeg(data, max_dim=max_dim)
    except Exception as e:
        return _err("load", f"failed to re-encode image: {e}")

    summary = {
        "ok": True,
        "source": label[:200],
        "mime": "image/jpeg",
        "bytes": len(payload),
        "inline": True,
        "note": "Image returned as an inline image block; clients without image "
                "rendering will only show this summary.",
    }
    return [
        TextContent(type="text", text=json.dumps(summary, ensure_ascii=False)),
        ImageContent(type="image", data=base64.b64encode(payload).decode(),
                     mimeType="image/jpeg"),
    ]


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

    # Optional image hosting (off unless ARK_UPLOAD_PROVIDER is set). Uploaded
    # public links land in share_urls so they survive a chat channel that only
    # renders text, such as the WeChat bot.
    hosting = describe_config()
    if hosting["enabled"] and local_paths:
        share_urls, notes = upload_paths(local_paths, notes)
        if share_urls:
            info["share_urls"] = share_urls
        info["hosting"] = {"provider": hosting["provider"]}
    elif hosting["enabled"]:
        info["hosting"] = {"provider": hosting["provider"], "detail": "no local files to upload"}

    if notes:
        info["notes"] = notes
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


@mcp.tool()
def ark_portrait_from_ref(
    image: str,
    style_ref: str,
    scene_profile: str,
    intent: Optional[str] = None,
    size: str = "2K",
    watermark: bool = False,
) -> ImageResult:
    """Generate a portrait of a person (from their identity template) in the style of a
    reference image.

    The identity comes from `image` (an identity template built by
    ark_generate_identity_templates — the face/body to keep) plus `scene_profile`
    (the identity profile text); the STYLE comes from `style_ref` (pose, outfit,
    scene, lighting to copy). The roles are fixed so the face is never swapped with
    the style reference's face.

    Args:
        image: Identity template image of the person (face/body to preserve)
        style_ref: Style reference image (pose/outfit/scene/lighting to follow)
        scene_profile: Identity profile text from ark_generate_identity_templates
        intent: Optional extra instructions (e.g. outfit/scene specifics)
        size: Image size. Square presets "1K"/"2K"/"4K" or "WIDTHxHEIGHT"
        watermark: Whether to add watermark
    """
    if not ARK_API_KEY:
        return _err("config", "ARK_API_KEY environment variable is not set.")
    if not image or not image.strip():
        return _err("invalid_argument", "image is required.")
    if not style_ref or not style_ref.strip():
        return _err("invalid_argument", "style_ref is required.")
    if not scene_profile or not scene_profile.strip():
        return _err("invalid_argument", "scene_profile is required.")
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
            notes.append(f"Identity template modified (dim >= {MAX_INPUT_DIM}px resized): {short_label}")
        processed = to_base64_data_uri(compressed)
    except Exception as e:
        return _err("load", f"failed to load identity template {image}: {e}")

    # style_ref 作为视觉记忆（context），身份图为主参考
    context_uris = []
    try:
        sdata, _ = load_input_image(style_ref)
        context_uris.append(_downscale_uri(sdata))
    except Exception as e:
        return _err("load", f"failed to load style reference {style_ref}: {e}")

    base_intent = (
        "生成一张写真：人物的脸与上半身身份必须严格来自第一张参考图（身份模板）中的人，"
        "严格保持其五官特征与身形，绝不要变成其他任何人的脸；"
        "姿势、构图、服装风格、场景氛围参考第二张风格参考图；"
        "整体自然真实的生活照质感，光线柔和，无伪影。"
    )
    if intent and intent.strip():
        base_intent += f"\n额外要求：{intent.strip()}"

    refined_prompt = None
    try:
        logger.info("Refining portrait prompt with vision model %s", ARK_VISION_MODEL)
        refined_prompt = refine_portrait_prompt(
            compressed, context_uris[0], scene_profile,
            (intent + "\n" + base_intent) if intent and intent.strip() else base_intent)
    except Exception as e:
        notes.append(f"Prompt refinement failed, using raw intent instead: {e}")

    payload = {
        "model": ARK_MODEL,
        "prompt": refined_prompt if refined_prompt else base_intent,
        "image": processed,
        "response_format": "url",
        "size": size,
        "watermark": watermark,
    }
    extra = {"refined_prompt": refined_prompt} if refined_prompt else None
    logger.info("Generating portrait: size=%s model=%s", size, ARK_MODEL)
    return _generate_images(payload, output_dir, notes, extra)


IDENTITY_TEMPLATE_PRESETS = [
    ("正面半身像", "正面朝向镜头，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("四十五度侧面半身像", "四十五度侧面朝向镜头，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("正面微笑半身像", "正面朝向镜头自然微笑，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("四十五度侧面微笑半身像", "四十五度侧面朝向镜头自然微笑，构图裁到腰部（上半身完整入画），双臂自然垂放或自然摆放，左右手臂都不被切出画面"),
    ("正面全身像", "正面朝向镜头全身入画，自然站姿，双臂自然垂放，手臂完整不被切出画面"),
]

_PRESET_SLUGS = {
    "正面半身像": "front_half",
    "四十五度侧面半身像": "side45_half",
    "正面微笑半身像": "front_smile",
    "四十五度侧面微笑半身像": "side45_smile",
    "正面全身像": "full_body",
}


def _preset_slug(desc: str) -> str:
    return _PRESET_SLUGS.get(desc, "tpl")


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
                # 重命名为可辨识的模板名：identity_tpl_<preset>_<ts>.jpg
                if tpl_file and os.path.exists(tpl_file):
                    slug = _preset_slug(desc)
                    new_name = f"identity_tpl_{slug}_{int(time.time())}.jpg"
                    new_path = os.path.join(os.path.dirname(tpl_file), new_name)
                    try:
                        os.rename(tpl_file, new_path)
                        tpl_file = new_path
                    except OSError:
                        pass  # 重命名失败则保留原文件名
                templates.append({"preset": desc, "file": tpl_file,
                                  "refined_prompt": out.get("refined_prompt")})
            else:
                templates.append({"preset": desc, "error": out.get("error")})
    finally:
        os.chdir(prev_cwd)

    return _ok(identity_profile=profile, identity_profile_md=md_path,
               anchor=os.path.basename(anchor_path),
               output_dir=output_dir, templates=templates, total=len(templates))


class _BearerAuthMiddleware:
    """Reject HTTP requests that do not carry an acceptable token.

    Tokens are matched per path: /mcp needs ARK_MCP_TOKEN, while /upload accepts
    ARK_UPLOAD_TOKEN as well (and falls back to the main token when no separate
    one is configured).

    A token may arrive two ways, because client support varies:
      * ``Authorization: Bearer <token>`` header (MCP Inspector, curl, Claude);
      * ``?token=<token>`` query parameter, for clients whose remote-MCP UI only
        offers a URL field.

    FastMCP's built-in auth support is OAuth-server oriented, which is heavy for
    a single-user server; a static token covers this use case.
    """

    def __init__(self, app, token: str, upload_token: str = "") -> None:
        self.app = app
        self.token = token
        self.upload_token = upload_token

    def _accepted(self, path: str) -> set:
        accepted = {t for t in (self.token,) if t}
        # The upload surface only writes/reads files in the input dir, so the
        # narrower upload token is enough there. Everything else needs the main one.
        if path.rstrip("/") in ("/upload", "/recent"):
            accepted.add(self.upload_token or self.token)
            accepted.discard("")
        return accepted

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        raw = headers.get(b"authorization", b"")
        supplied = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) else str(raw)
        bearer = supplied[7:] if supplied.lower().startswith("bearer ") else ""

        query = scope.get("query_string", b"")
        if isinstance(query, (bytes, bytearray)):
            query = query.decode("latin-1")
        params = urllib.parse.parse_qs(str(query))
        query_tokens = set(params.get("token", []))

        accepted = self._accepted(scope.get("path", ""))
        if bearer and bearer in accepted:
            return await self.app(scope, receive, send)
        if query_tokens & accepted:
            return await self.app(scope, receive, send)

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer")],
        })
        await send({"type": "http.response.body",
                    "body": b'{"error":"unauthorized"}'})


def _input_dir() -> str:
    d = ARK_INPUT_DIR or os.path.join(tempfile.gettempdir(), "ark-image-inputs")
    os.makedirs(d, exist_ok=True)
    return d


def _save_upload_bytes(body: bytes, prefix: str = "input") -> dict:
    """Validate image bytes, write them under the input dir. Returns a report dict."""
    limit = ARK_MAX_UPLOAD_MB * 1024 * 1024
    if len(body) > limit:
        return {"ok": False, "error": f"payload exceeds {ARK_MAX_UPLOAD_MB} MB", "_status": 413}

    ext = ".jpg"
    if HAS_PIL:
        try:
            probe = PILImage.open(io.BytesIO(body))
            probe.verify()
            fmt = (probe.format or "JPEG").upper()
            ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                   "GIF": ".gif", "BMP": ".bmp"}.get(fmt, ".jpg")
        except Exception as e:
            return {"ok": False, "error": f"not a decodable image: {e}", "_status": 400}

    name = f"{prefix}_{uuid.uuid4().hex}{ext}"
    path = os.path.join(_input_dir(), name)
    try:
        with open(path, "wb") as fh:
            fh.write(body)
    except OSError as e:
        return {"ok": False, "error": f"could not write upload: {e}", "_status": 500}

    logger.info("Received upload: %s (%d bytes)", name, len(body))
    return {"ok": True, "path": path, "bytes": len(body), "filename": name}


def _recent_uploads(limit: int = 5) -> list:
    """Newest uploaded images on disk, newest first."""
    try:
        names = os.listdir(_input_dir())
    except OSError:
        return []
    entries = []
    for name in names:
        full = os.path.join(_input_dir(), name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        entries.append({"path": full, "filename": name, "bytes": st.st_size,
                        "uploaded_at": int(st.st_mtime)})
    entries.sort(key=lambda e: e["uploaded_at"], reverse=True)
    return entries[:max(1, min(int(limit or 5), 20))]


async def _handle_upload(request):
    """POST /upload — accept raw image bytes and park them on this machine."""
    from starlette.responses import JSONResponse

    declared = request.headers.get("content-length")
    limit = ARK_MAX_UPLOAD_MB * 1024 * 1024
    if declared and declared.isdigit() and int(declared) > limit:
        return JSONResponse({"ok": False, "error": f"payload exceeds {ARK_MAX_UPLOAD_MB} MB"},
                            status_code=413)

    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "error": "empty body"}, status_code=400)

    report = _save_upload_bytes(body)
    status = report.pop("_status", 200)
    if report.get("ok"):
        report["hint"] = "Pass this absolute path as the `image` argument."
    return JSONResponse(report, status_code=status)


_UPLOAD_PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>上传图片 · ark-image</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin:0; padding:24px 18px 60px; font:16px/1.5 -apple-system,
        BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
        background:#15171c; color:#e7e9ee; }
 h1 { font-size:19px; margin:0 0 4px; }
 p.sub { color:#8b93a3; font-size:13px; margin:0 0 22px; }
 .drop { display:block; border:2px dashed #3a4150; border-radius:14px;
         padding:34px 16px; text-align:center; color:#98a1b3; margin-bottom:16px; }
 .drop.has { border-color:#4b8bf5; color:#c9d6ee; }
 input[type=file] { display:none; }
 button { width:100%; padding:15px; border:0; border-radius:12px;
          font-size:16px; font-weight:600; background:#2f6fed; color:#fff; }
 button:disabled { background:#2a3242; color:#69748a; }
 .msg { margin-top:18px; padding:14px; border-radius:12px; font-size:14px;
        white-space:pre-wrap; word-break:break-all; display:none; }
 .msg.err { background:#3a1e22; color:#ffb4b4; display:block; }
 .msg.ok  { background:#16301f; color:#a8e6bd; display:block; }
 .path { margin-top:10px; padding:12px; border-radius:10px; background:#0e1116;
         border:1px solid #2a3242; font:13px/1.45 ui-monospace,Menlo,monospace;
         color:#9fc2ff; word-break:break-all; }
 .copy { margin-top:8px; padding:10px; font-size:14px; background:#243044; }
 h2 { font-size:14px; color:#8b93a3; margin:30px 0 10px; font-weight:600; }
 .recent { list-style:none; margin:0; padding:0; }
 .recent li { padding:11px 12px; border-radius:10px; background:#1b1f27;
              margin-bottom:8px; font-size:13px; display:flex;
              justify-content:space-between; gap:10px; align-items:center; }
 .recent .name { min-width:0; flex:1 1 auto; }
 .recent .name b { font-weight:500; display:block; overflow:hidden;
                   text-overflow:ellipsis; white-space:nowrap; }
 .recent span { color:#7d8698; font:12px ui-monospace,monospace; }
 .recent button { width:auto; flex:0 0 auto; white-space:nowrap;
                  padding:7px 12px; font-size:13px; background:#2a3242; }
</style></head><body>
<h1>上传图片</h1>
<p class="sub">选一张照片传到这里，然后把它给的路径发进对话。</p>

<label class="drop" id="drop">
  <input type="file" id="file" accept="image/*">
  <div id="pick">点击选择照片</div>
</label>
<button id="go" disabled>上传</button>

<div class="msg" id="msg"></div>

<h2>最近上传</h2>
<ul class="recent" id="recent"><li><span>加载中…</span></li></ul>

<script>
const TOKEN = __TOKEN__;
const fileInput = document.getElementById('file');
const drop = document.getElementById('drop');
const pick = document.getElementById('pick');
const go = document.getElementById('go');
const msg = document.getElementById('msg');

fileInput.addEventListener('change', () => {
  const f = fileInput.files[0];
  if (!f) return;
  pick.textContent = f.name + '  (' + (f.size/1024).toFixed(0) + ' KB)';
  drop.classList.add('has');
  go.disabled = false;
});

function show(html, cls) {
  msg.innerHTML = html;
  msg.className = 'msg ' + cls;
  msg.scrollIntoView({behavior:'smooth', block:'nearest'});
}

go.addEventListener('click', async () => {
  const f = fileInput.files[0];
  if (!f) return;
  go.disabled = true;
  go.textContent = '上传中…';
  try {
    const r = await fetch('/upload?token=' + encodeURIComponent(TOKEN), {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + TOKEN,
                'Content-Type': f.type || 'application/octet-stream'},
      body: f
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.error || ('HTTP ' + r.status));
    show('<b>上传成功</b><div class="path">' + j.path + '</div>' +
         '<button class="copy" onclick="copyPath(this, \\'' + j.path.replace(/\\\\/g,'\\\\\\\\') + '\\')">复制路径</button>', 'ok');
    loadRecent();
  } catch (e) {
    show('上传失败：' + e.message, 'err');
  } finally {
    go.disabled = false;
    go.textContent = '上传';
  }
});

function copyPath(btn, p) {
  const done = () => { btn.textContent = '已复制'; };
  if (navigator.clipboard) { navigator.clipboard.writeText(p).then(done); }
  else {
    const t = document.createElement('textarea');
    t.value = p; document.body.appendChild(t); t.select();
    document.execCommand('copy'); t.remove(); done();
  }
}

function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(0) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}

async function loadRecent() {
  try {
    const r = await fetch('/recent?token=' + encodeURIComponent(TOKEN));
    const j = await r.json();
    const ul = document.getElementById('recent');
    if (!j.ok || !j.files.length) { ul.innerHTML = '<li><span>还没有上传过图片</span></li>'; return; }
    ul.innerHTML = j.files.map(f =>
      '<li><div class="name"><b>' + f.filename + '</b><span>' + fmtSize(f.bytes) +
      '</span></div><button onclick="copyPath(this, \\'' +
      f.path.replace(/\\\\/g,'\\\\\\\\') + '\\')">复制</button></li>'
    ).join('');
  } catch (e) {}
}
loadRecent();
</script>
</body></html>
"""


async def _handle_upload_page(request):
    """GET /upload — a phone-friendly page that uploads with the real browser.

    The point is to keep image bytes away from the model entirely: a phone
    sandbox may have no shell and can only emit small tool arguments, but its
    browser can POST a 5 MB photo without any trouble.
    """
    from starlette.responses import HTMLResponse

    token = ARK_UPLOAD_TOKEN or ARK_MCP_TOKEN
    return HTMLResponse(_UPLOAD_PAGE.replace("__TOKEN__", json.dumps(token)))


async def _handle_recent(request):
    """GET /recent — recent uploads as JSON, used by the page and by clients."""
    from starlette.responses import JSONResponse

    return JSONResponse({"ok": True, "files": _recent_uploads(5)})



def _build_http_app():
    """Add the /upload side-route to the MCP app.

    The route is inserted into the MCP app's own router rather than wrapping it
    in a parent Starlette app: wrapping drops the app's lifespan, which is what
    starts the streamable-HTTP session manager, and every request then 500s.
    """
    from starlette.routing import Route

    if ARK_TRANSPORT == "sse":
        return mcp.sse_app()

    inner = mcp.streamable_http_app()
    inner.router.routes.insert(0, Route("/recent", _handle_recent, methods=["GET"]))
    inner.router.routes.insert(0, Route("/upload", _handle_upload_page, methods=["GET"]))
    inner.router.routes.insert(0, Route("/upload", _handle_upload, methods=["POST"]))
    return inner


def main() -> None:
    if ARK_TRANSPORT == "stdio":
        mcp.run()
        return

    if ARK_TRANSPORT not in ("http", "streamable-http", "sse"):
        raise SystemExit(
            f"unsupported ARK_TRANSPORT '{ARK_TRANSPORT}' (expected stdio|http|sse)"
        )
    if not ARK_MCP_TOKEN:
        raise SystemExit(
            "ARK_MCP_TOKEN is required when serving over HTTP: an unauthenticated "
            "endpoint would let anyone spend your Ark credits."
        )

    import uvicorn

    path = "/sse" if ARK_TRANSPORT == "sse" else ARK_HTTP_PATH
    app = _build_http_app()
    logger.info("Serving MCP over %s at http://%s:%s%s (bearer auth required)",
                ARK_TRANSPORT, ARK_HOST, ARK_PORT, path)
    if ARK_TRANSPORT != "sse":
        logger.info("Upload endpoint: POST http://%s:%s/upload (same bearer token)",
                    ARK_HOST, ARK_PORT)
    uvicorn.run(_BearerAuthMiddleware(app, ARK_MCP_TOKEN, ARK_UPLOAD_TOKEN),
                host=ARK_HOST, port=ARK_PORT, log_level="info")


if __name__ == "__main__":
    main()
