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

[MIT](LICENSE)
