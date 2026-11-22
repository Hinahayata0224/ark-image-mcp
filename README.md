# ark-image-mcp

MCP server for 火山方舟 (Volcano Ark) **Doubao-Seedream** image generation and editing.

A single-file [MCP](https://modelcontextprotocol.io) server that lets any MCP client
(model hosts like Claude, ZCode, etc.) generate and edit images through Volcano Ark's
Seedream image models — with a vision model used to read reference images and write
faithful, detailed prompts.

> **Design intent** — *give a smart blind model multimodal superpowers.*
> Most coding agents are text-only: they cannot see the images in your workspace, so
> they cannot edit them. This server acts as their eyes and hands — the vision model
> describes what is in a photo, the editing model redraws it, and the agent only ever
> deals with text (a path, an intent, a returned JSON). That way a model with zero
> vision capability can still "look at" and modify images as if it could see them.

## Features

- **Text-to-image** and **image-to-image** generation (`ark_generate_image`).
- **Intent-driven editing** (`ark_edit_image`): describe the change in natural language;
  a vision model rewrites it into a detailed English prompt, then Seedream renders.
- **Revision loop** (`ark_revise_image`): pass your review feedback on a previous result;
  only the listed issues are fixed, everything else is preserved.
- **Scene profiles** (`ark_scene_profile`): build a reusable description from multiple
  photos of the same scene/person, then pass it into edits for consistency.
- EXIF rotation correction, large-image downscaling, retries with backoff, and
  structured JSON responses (`ok` / `error_type`).
- **Optional image hosting**: upload results to a private bucket and return
  time-limited signed URLs, so images can be opened from a phone or a chat
  channel that only renders text. Disabled unless you configure it.

## Models

| Role | Model | Override env var |
|---|---|---|
| Generation / editing | `doubao-seedream-5-0-pro-260628` | `ARK_MODEL` |
| Vision (prompt writing, profiles, revisions) | `doubao-seed-2-1-pro-260628` | `ARK_VISION_MODEL` |

## Requirements

- Python 3.10+
- A Volcano Ark API key with access to the models above (create it in the Volcano Ark console).

## Setup

```bash
uv sync                 # installs dependencies incl. dev tools (pytest)
cp .env.example .env    # then fill in ARK_API_KEY
```

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `ARK_API_KEY` | *(required)* | Volcano Ark API key |
| `ARK_MODEL` | `doubao-seedream-5-0-pro-260628` | Generation model |
| `ARK_VISION_MODEL` | `doubao-seed-2-1-pro-260628` | Vision (chat) model |
| `ARK_TIMEOUT` | `300` | Generation request timeout (s) |

### Optional image hosting

Set `ARK_UPLOAD_PROVIDER` to also upload every generated image and get back a
`share_urls` list in the response. Local files are still saved exactly as before,
and any upload error is reported in `notes` instead of failing the request.

**`aliyun`** — Alibaba Cloud OSS with a **private** bucket. The bucket's bare URLs
are useless; each returned link is signed and expires after `ARK_OSS_URL_TTL`
(7 days by default). Create the bucket with *Block Public Access* **on** and a
private ACL.

| Variable | Description |
|---|---|
| `ARK_UPLOAD_PROVIDER` | `aliyun`, `cloudinary`, or `smms` |
| `ARK_OSS_ACCESS_KEY_ID` / `ARK_OSS_ACCESS_KEY_SECRET` | RAM credentials |
| `ARK_OSS_BUCKET` | Bucket name |
| `ARK_OSS_ENDPOINT` | e.g. `https://oss-cn-shenzhen.aliyuncs.com` |
| `ARK_OSS_PREFIX` | Object key prefix (default `ark/`) |
| `ARK_OSS_URL_TTL` | Signed URL lifetime in seconds (default `604800`) |

Use a RAM sub-account whose policy is limited to this bucket rather than an
account-wide AccessKey.

**`cloudinary`** — unsigned upload preset. Set `ARK_CLOUDINARY_PRESET` (and
optionally `ARK_CLOUDINARY_CLOUD`). Note that Cloudinary assets are **public by
default**; this backend is not appropriate for private images.

**`smms`** — SM.MS v2 API. Set `ARK_SMMS_TOKEN`. SM.MS is no longer keyless; the
v1 endpoint is retired.

## MCP client setup (stdio)

```json
{
  "mcpServers": {
    "ark-image-gen": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/ark-image-mcp/server.py"],
      "env": { "ARK_API_KEY": "ark-..." }
    }
  }
}
```

> The server also exposes a full usage manual through MCP `instructions` (returned on
> `initialize`) — clients/models that read it can use the tools without extra docs.

## Remote setup (HTTP transport)

For clients that cannot spawn a local process — a phone app such as Chatbox, a
browser, or a machine other than the one running the server — serve the same
tools over **streamable HTTP**:

| Variable | Default | Description |
|---|---|---|
| `ARK_TRANSPORT` | `stdio` | `stdio` (local) or `http` (remote). `sse` also accepted for older clients |
| `ARK_HOST` | `127.0.0.1` | Bind address; use `0.0.0.0` to accept LAN/tunnel traffic |
| `ARK_PORT` | `8765` | Listen port |
| `ARK_HTTP_PATH` | `/mcp` | Endpoint path (`/sse` when `ARK_TRANSPORT=sse`) |
| `ARK_MCP_TOKEN` | *(required for http)* | Bearer token every request must present |
| `ARK_UPLOAD_TOKEN` | falls back to `ARK_MCP_TOKEN` | Upload-only credential for `POST /upload` |
| `ARK_PUBLIC_BASE_URL` | derived from host/port | Public URL, used when wording instructions for remote clients |
| `ARK_INPUT_DIR` | temp dir | Where `POST /upload` writes received images |
| `ARK_MAX_UPLOAD_MB` | `25` | Max upload body size |

```bash
ARK_API_KEY=ark-... \
ARK_TRANSPORT=http ARK_HOST=0.0.0.0 ARK_PORT=8765 \
ARK_MCP_TOKEN=$(openssl rand -hex 24) \
python server.py
```

Requests must carry the token either as a header (`Authorization: Bearer <token>`)
or as a query parameter (`/mcp?token=<token>`) — the latter exists because some
mobile clients (Chatbox) only offer a URL field. Anything else gets `401`. The
server **refuses to start** in HTTP mode without a token, because an open
endpoint lets anyone spend your Ark credits.

Client side (Chatbox and similar): choose the remote/streamable-HTTP transport,
set the URL to `https://<your-host>/mcp`, and add a header
`Authorization=Bearer <token>` if the client offers an HTTP-header field
(Chatbox's mobile app does). Clients that only accept a URL can use
`https://<your-host>/mcp?token=<token>` instead; a query token is easier to leak
into proxy logs, so keep the URL private and always use HTTPS.

Two things behave differently from stdio:

- **Image inputs must be URLs, not local paths.** A path like `F:\photo.jpg`
  refers to the *server's* filesystem, which the phone cannot see.
- **Output files land on the server's disk.** Configure `ARK_UPLOAD_PROVIDER`
  (see above) so results also come back as `share_urls` you can open anywhere.

### Images that only exist on the client

When the server runs on a different machine than the client, a photo the user
attached on their phone lives in the *client's* workspace and is unreadable here.
Three ways to bridge that, in order of practicality:

1. **`POST /upload`** — send the raw bytes, get back an absolute server path:

   ```bash
   curl -sS -X POST 'https://your-host/upload' \
        -H 'Authorization: Bearer <ARK_UPLOAD_TOKEN>' \
        --data-binary @photo.jpg
   # -> {"ok":true,"path":"C:\\...\\input_ab12.jpg","bytes":363258}
   ```

   Give the returned `path` to any editing tool. The body must decode as an
   image (JPEG/PNG/WebP/GIF/BMP) and the extension is derived from the real
   format, not from your filename.

   `ARK_UPLOAD_TOKEN` exists so a remote model can upload without holding the
   MCP session credential: it authorizes **only** `/upload`, never tool calls.
   The token is embedded in the MCP instructions so the model can build the
   curl command itself — treat it as semi-public within your client.

2. **`data:` URL** — `data:image/jpeg;base64,<payload>`, passed straight as an
   `image` argument. Fine for small images; a 1 MB photo becomes ~1.4 M
   characters of tool argument, which is expensive.

3. **`b64:<payload>`** — the same bytes without the `data:` prefix.

Relative paths are rejected with a message pointing at `/upload` rather than
failing deep inside an HTTP client.

Expose it over HTTPS before using it from a phone — put it behind a reverse
proxy, a tunnel (Cloudflare Tunnel, ngrok, Tailscale Funnel), or a cloud VM
with a TLS-terminating proxy. Plain HTTP over the public internet leaks the
token and the images.

### Showing images in the chat

Many MCP clients do **not** render native `image` content blocks. Chatbox is one
of them: it prints the raw base64 into the conversation, which both hides the
picture and burns tens of thousands of tokens.

Use `ark_show_image` with its default `mode="markdown"` instead. It re-encodes
the picture, uploads it via the configured image host, and returns a line like
`![image](https://...)`; repeat that line in your reply and any Markdown-based UI
renders the picture inline. `mode="block"` exists for clients that genuinely
render MCP images — do not use it with Chatbox.

### Cloudflare Tunnel (worked example)

Prerequisites: a domain whose nameservers point at Cloudflare, and `cloudflared`.

```bash
cloudflared tunnel login
cloudflared tunnel create ark-image
cloudflared tunnel route dns ark-image mcp.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id-from-create>
credentials-file: /path/to/<tunnel-id>.json
ingress:
  - hostname: mcp.example.com
    service: http://127.0.0.1:8765
  - service: http_status:404
```

```bash
cloudflared tunnel run ark-image
```

`run-public.bat` (Windows) does steps 2–3 of the launch for you: it loads
`.env.http`, starts the Python server, then runs the tunnel. Machine-specific
paths (`CLOUDFLARED_BIN`, `TUNNEL_CONFIG`, `TUNNEL_NAME`) are read from
`.env.http` so the script itself stays portable. Note the tunnel is an outbound
connection from your machine — the service disappears when the machine sleeps.


## Client setup (works in ZCode, OpenCode, Codex)

Because this is a standard stdio MCP server, it plugs into any MCP-compatible client.
Below are the three common setups (adjust paths to your install).

**ZCode** — add to the `mcp.servers` block of your ZCode config:

```json
{
  "mcp": {
    "servers": {
      "ark-image-gen": {
        "type": "stdio",
        "command": "C:\\path\\to\\ark-image-mcp\\.venv\\Scripts\\python.exe",
        "args": ["C:\\path\\to\\ark-image-mcp\\server.py"],
        "env": { "ARK_API_KEY": "ark-..." }
      }
    }
  }
}
```

**OpenCode** — in `opencode.json` (project or global):

```json
{
  "mcp": {
    "ark-image-gen": {
      "type": "local",
      "command": ["/path/to/ark-image-mcp/.venv/bin/python", "/path/to/ark-image-mcp/server.py"],
      "environment": { "ARK_API_KEY": "ark-..." },
      "enabled": true
    }
  }
}
```

**Codex** — in `~/.codex/config.toml`:

```toml
[mcp.servers.ark-image-gen]
command = "/path/to/ark-image-mcp/.venv/bin/python"
args = ["/path/to/ark-image-mcp/server.py"]
env = { ARK_API_KEY = "ark-..." }
```

> The tools are then available to the agent as normal MCP tools
> (`ark_generate_image`, `ark_edit_image`, `ark_revise_image`, `ark_scene_profile`).
> Even a model with no vision capability can "see" and edit images through these tools.

## Tools

| Tool | Purpose | Key args |
|---|---|---|
| `ark_scene_profile` | Build a scene profile from multiple same-scene photos | `images`, `focus` |
| `ark_analyze_image` | Ask a targeted question about an image (or a region) | `image`, `query`, `region` |
| `ark_locate_object` | Locate an object, returning a bounding box | `image`, `object_desc` |
| `ark_edit_image` | Edit a single reference image from an intent | `intent`, `image`, `size`, `scene_profile`, `region`, `mask`, `context_images` |
| `ark_revise_image` | Iterate on a previous result from feedback | `revision`, `image`, `reference`, `size`, `scene_profile` |
| `ark_capture_screen` | Capture the screen (or a region) as a PNG | `region`, `max_dim` |
| `ark_verify_edit` | Verify an edit against the original + intent (QA loop) | `original`, `edited`, `intent` |
| `ark_generate_identity_templates` | Build standardized identity template images from many photos | `images`, `count` |
| `ark_generate_image` | Direct text/image-to-image generation | `prompt`, `image`, `size`, `response_format` |

### Spatial & memory features

- **Region-scoped edits**: pass `region="[x1,y1,x2,y2]"` (0-1000 normalized) to
  `ark_edit_image` to restrict the change to that area — a mask is generated
  (white = edit area) and everything outside is preserved.
- **Custom mask**: pass `mask=<path/url>` to `ark_edit_image` for a hand-drawn mask
  (white = edit, black = preserve).
- **Visual memory**: pass `context_images="a.jpg,b.jpg"` (same-scene photos) to
  `ark_edit_image`; they are shown to the vision model during prompt refinement so
  outfit/scene details stay consistent even when the reference is cropped or blurry.
- **QA loop**: after an edit, `ark_verify_edit(original, edited, intent)` returns
  `{passed, reasons, summary}` so the caller can decide whether to retry.

### Visual inspection loop (screen capture)

`ark_capture_screen` lets the agent take a screenshot of the software it is
working on and feed it back into the perception tools — a self-contained
"look, judge, continue" loop:

```
work on the software
  → ark_capture_screen(region?)          # capture current UI state
  → ark_analyze_image(<screenshot>, "is this correct?")   # see it
  → ark_locate_object(<screenshot>, "save button")        # find UI elements
  → ark_verify_edit(<before>, <after>, intent)            # confirm a change
  → continue the workflow
```

Screenshots are saved as `ark_screen_*.png` in the caller's working directory.
`region="[x1,y1,x2,y2]"` (0-1000 normalized) captures only part of the screen.
Requires a platform with `PIL.ImageGrab` (Windows/macOS); on others the tool
returns a clear error.

### Identity templates → portraits (person-consistent generation)

`ark_generate_identity_templates(images, count)` builds **standardized identity
template images** from many photos of one person (like the Doubao app's avatar
feature, but with leakage control):

1. The vision model reads the photos, writes a detailed identity profile
   (facial features, body, hair, distinctive traits), and picks the best
   front-facing **anchor** photo.
2. For each preset (front headshot, three-quarter, side, full body, smiling),
   the anchor is the main reference + the other photos are visual memory +
   the identity profile is the text lock — generated with a **neutral light-grey
   background and plain white top**, so clothing/background leakage is avoided
   at the source (the Doubao approach's known weakness).
3. **Human review**: pick the best 2-4 templates, then reuse them as
   `image` (with `scene_profile=identity_profile`) in `ark_edit_image` to
   generate portraits of that person in any outfit/scene.

> Note: this is "reference-based identity distillation", not a trained LoRA.
> Identity fidelity is strongest when the target pose/framing is close to the
> template; extreme pose changes (back view, strong side) degrade fidelity.

### Recommended workflow

1. `ark_scene_profile(images="a.jpg,b.jpg")` — build the scene profile once per scene.
2. `ark_edit_image(intent=..., image="a.jpg", scene_profile=...)` — make the edit.
3. Review the result; if something is off: `ark_revise_image(revision=..., image=<result>, reference="a.jpg", scene_profile=...)`.
4. Repeat step 3 as needed.

### Size constraint

`size` accepts the square presets `1K` / `2K` / `4K`, or an explicit `WIDTHxHEIGHT`
where each side is 512–4096 **and** the area is ≥ 921600 px (Seedream's editing
minimum). Portrait phone photos are common — use e.g. `1024x1536`.

### Responses

All tools return a JSON object:

- Success: `{"ok": true, "files": [...], "usage": {...}, "notes": [...], "refined_prompt": "..."}`
  (`refined_prompt` is the prompt the vision model actually wrote — check it to verify
  intent fidelity).
- Failure: `{"ok": false, "error_type": "config|invalid_argument|load|api", "error": "..."}`

`ark_scene_profile` returns `{"ok": true, "profile": "...", "images": N}` on success.

Output images are saved to the caller's current working directory as `ark_img_*.jpg`.

## Development

```bash
uv run pytest          # offline unit tests (no API calls)
```

Run an end-to-end check (requires `ARK_API_KEY`):

```bash
uv run python -c "import server; print(server.ark_generate_image('a red apple'))"
```

## License

[GPL-3.0](LICENSE)
