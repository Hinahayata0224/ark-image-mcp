"""Optional image-hosting backends for ark-image-gen.

This module is entirely optional. Nothing here runs unless ARK_UPLOAD_PROVIDER is
set in the environment, and every failure is non-fatal: generation still succeeds
and you still get the local file paths.

Supported providers
-------------------
cloudinary : unsigned upload preset. Needs only a preset name (and optionally a
              cloud name), because the preset itself carries the account context.
              Create an *unsigned* preset in the Cloudinary console under
              Settings -> Upload -> Upload presets, then export:
                  ARK_UPLOAD_PROVIDER=cloudinary
                  ARK_CLOUDINARY_PRESET=<preset name>
                  ARK_CLOUDINARY_CLOUD=<cloud name>   # optional, speeds up the URL
              Uploaded files land in a folder named after the preset, so they stay
              easy to find (and easy to bulk-delete) in the media library.

smms        : SM.MS v2 API. Needs a token obtained from a registered account:
                  ARK_UPLOAD_PROVIDER=smms
                  ARK_SMMS_TOKEN=<token>
              Note that SM.MS is no longer a keyless free host; the v1 endpoint is
              retired and v2 rejects anonymous uploads.

aliyun      : Alibaba Cloud OSS with a PRIVATE bucket. Nothing is publicly
              reachable: every URL handed back is a signed, time-limited link
              generated with the AccessKey, so the bytes are served only to
              holders of a valid link and the link itself expires.
                  ARK_UPLOAD_PROVIDER=aliyun
                  ARK_OSS_ACCESS_KEY_ID=<AccessKey ID>
                  ARK_OSS_ACCESS_KEY_SECRET=<AccessKey Secret>
                  ARK_OSS_BUCKET=<bucket name, e.g. my-images>
                  ARK_OSS_ENDPOINT=<endpoint, e.g. https://oss-cn-hangzhou.aliyuncs.com>
                  ARK_OSS_PREFIX=<optional key prefix, default 'ark/'>
                  ARK_OSS_URL_TTL=<optional signed-URL lifetime in seconds, default 604800 (7 days)>
              Requires the 'oss2' package:  pip install oss2
              Use an STS/RAM sub-account key with a policy limited to this bucket
              rather than the account's primary AccessKey.

Usage
-----
    from uploader import upload_paths, describe_config

    uploaded, notes = upload_paths(["F:/dir/ark_img_1.jpg", ...])
    # uploaded is a list of public URLs, or [] if disabled / nothing succeeded
"""

import os
import uuid
from typing import Optional

import requests

try:  # Pillow is optional here; only used to sniff the format for SM.MS
    from PIL import Image as _PILImage
except ImportError:  # pragma: no cover
    _PILImage = None

DEFAULT_TIMEOUT = int(os.environ.get("ARK_UPLOAD_TIMEOUT", "120"))

SUPPORTED_PROVIDERS = ("aliyun", "cloudinary", "smms")

# Env vars each provider needs before it can be used, mapped to a human label.
_REQUIRED_ENV = {
    "cloudinary": (("ARK_CLOUDINARY_PRESET",), "ARK_CLOUDINARY_PRESET"),
    "smms": (("ARK_SMMS_TOKEN",), "ARK_SMMS_TOKEN"),
    "aliyun": (
        ("ARK_OSS_ACCESS_KEY_ID", "ARK_OSS_ACCESS_KEY_SECRET",
         "ARK_OSS_BUCKET", "ARK_OSS_ENDPOINT"),
        "ARK_OSS_ACCESS_KEY_ID / ARK_OSS_ACCESS_KEY_SECRET / ARK_OSS_BUCKET / ARK_OSS_ENDPOINT",
    ),
}


def _enabled() -> Optional[str]:
    """Return the configured provider name, or None when hosting is disabled."""
    provider = os.environ.get("ARK_UPLOAD_PROVIDER", "").strip().lower()
    return provider or None


def describe_config() -> dict:
    """Report the hosting configuration. Safe to call when nothing is configured."""
    provider = _enabled()
    info = {"enabled": bool(provider), "provider": provider, "ok": True}
    if not provider:
        info["detail"] = "not configured (set ARK_UPLOAD_PROVIDER to enable)"
        return info
    if provider not in _REQUIRED_ENV:
        info["ok"] = False
        info["detail"] = f"unknown provider '{provider}' (expected one of {', '.join(SUPPORTED_PROVIDERS)})"
        return info
    keys, _ = _REQUIRED_ENV[provider]
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if missing:
        info["ok"] = False
        info["detail"] = f"missing env: {', '.join(missing)}"
    else:
        info["detail"] = "configured"
    return info


def _upload_cloudinary(path: str) -> str:
    preset = os.environ.get("ARK_CLOUDINARY_PRESET", "").strip()
    if not preset:
        raise RuntimeError("ARK_CLOUDINARY_PRESET is not set")
    url = "https://api.cloudinary.com/v1_1/image/upload"
    with open(path, "rb") as fh:
        files = {"file": fh}
        data = {
            "upload_preset": preset,
            "folder": f"ark_image_gen/{preset}",
            "public_id": uuid.uuid4().hex,
        }
        cloud = os.environ.get("ARK_CLOUDINARY_CLOUD", "").strip()
        if cloud:
            url = f"https://api.cloudinary.com/v1_1/{cloud}/image/upload"
        resp = requests.post(url, files=files, data=data, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    # Cloudinary returns public_id (without extension); prefer secure_url.
    return payload.get("secure_url") or payload.get("url") or ""


def _upload_smms(path: str) -> str:
    token = os.environ.get("ARK_SMMS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ARK_SMMS_TOKEN is not set")
    endpoint = "https://smms.app/api/v2/upload"
    headers = {"Authorization": f"Bearer {token}"}
    with open(path, "rb") as fh:
        resp = requests.post(
            endpoint,
            headers=headers,
            files={"smfile": fh},
            data={"format": "json"},
            timeout=DEFAULT_TIMEOUT,
        )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(payload.get("message") or "SM.MS upload failed")
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("unexpected SM.MS response shape")
    return data.get("url", "")


def _oss_bucket():
    """Build an oss2 Bucket from the environment, or raise a readable error."""
    try:
        import oss2  # imported lazily so the other backends don't need it
    except ImportError:
        raise RuntimeError("the 'oss2' package is required for the aliyun provider "
                           "(pip install oss2)")

    def _env(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set")
        return value

    endpoint = _env("ARK_OSS_ENDPOINT")
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    auth = oss2.Auth(_env("ARK_OSS_ACCESS_KEY_ID"), _env("ARK_OSS_ACCESS_KEY_SECRET"))
    return oss2.Bucket(auth, endpoint, _env("ARK_OSS_BUCKET"))


def _upload_aliyun(path: str) -> str:
    """Upload to a private OSS bucket and return a signed, expiring URL.

    The bucket is expected to be private, so the bare object URL is useless
    without a signature; we therefore never return it. The signed URL we hand
    back expires after ARK_OSS_URL_TTL seconds (7 days by default).
    """
    bucket = _oss_bucket()
    prefix = os.environ.get("ARK_OSS_PREFIX", "ark/").strip()
    ttl = int(os.environ.get("ARK_OSS_URL_TTL", str(7 * 24 * 3600)))
    key = f"{prefix}{uuid.uuid4().hex}{os.path.splitext(path)[1] or '.jpg'}"

    with open(path, "rb") as fh:
        bucket.put_object(key, fh)

    # A signed GET URL. The bucket is private, so this signature is the only way
    # to read the object, and it stops working once ttl seconds have passed.
    return bucket.sign_url("GET", key, ttl, slash_safe=True)


_UPLOADERS = {
    "cloudinary": _upload_cloudinary,
    "smms": _upload_smms,
    "aliyun": _upload_aliyun,
}


def upload_paths(paths, notes: Optional[list] = None) -> tuple:
    """Upload local images and return (public_urls, notes).

    Never raises. When hosting is disabled or a provider is misconfigured, this
    returns ([], []) so the caller can keep going with local files only.
    """
    notes = notes if notes is not None else []
    provider = _enabled()
    if not provider:
        return [], notes
    uploader = _UPLOADERS.get(provider)
    if uploader is None:
        notes.append(f"Image hosting skipped: unknown provider '{provider}'.")
        return [], notes

    urls = []
    for path in paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            url = uploader(path)
            if url:
                urls.append(url)
            else:
                notes.append(f"Image hosting: {os.path.basename(path)} returned no URL.")
        except Exception as e:  # hosting is a convenience, never a hard failure
            notes.append(f"Image hosting failed for {os.path.basename(path)}: {e}")
    return urls, notes
