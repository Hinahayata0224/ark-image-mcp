# ark-image-mcp

MCP server for 火山方舟 (Volcano Ark) **Doubao-Seedream** image generation and editing.

A single-file [MCP](https://modelcontextprotocol.io) server that lets any MCP client
(model hosts like Claude, ZCode, etc.) generate and edit images through Volcano Ark's
Seedream image models — with a vision model used to read reference images and write
faithful, detailed prompts.

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

## Tools

| Tool | Purpose | Key args |
|---|---|---|
| `ark_scene_profile` | Build a scene profile from multiple same-scene photos | `images`, `focus` |
| `ark_edit_image` | Edit a single reference image from an intent | `intent`, `image`, `size`, `scene_profile` |
| `ark_revise_image` | Iterate on a previous result from feedback | `revision`, `image`, `reference`, `size`, `scene_profile` |
| `ark_generate_image` | Direct text/image-to-image generation | `prompt`, `image`, `size`, `response_format` |

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
