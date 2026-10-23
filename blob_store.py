"""Vercel Blob storage helpers.

Serverless functions have no persistent disk between invocations, so every piece of
generated output (manifest, scripts, audio, captions, video, QR codes) lives in Vercel
Blob under the `issues/<issue_key>/...` prefix instead of the local `work/` folder.
Endpoints pull what they need into a scratch directory under /tmp, run the existing
(unmodified) pipeline functions against it, then push the results back here.

Requires the BLOB_READ_WRITE_TOKEN env var (auto-injected by Vercel's Blob integration).
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

BLOB_API = "https://blob.vercel-storage.com"


def _token() -> str:
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set (attach the Vercel Blob integration)")
    return token


def _headers(extra: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {_token()}", "x-api-version": "7"}
    if extra:
        headers.update(extra)
    return headers


def put_bytes(pathname: str, data: bytes, content_type: str | None = None) -> str:
    """Upload bytes to a stable pathname (no random suffix). Returns the public URL."""
    headers = _headers({"x-add-random-suffix": "0"})
    if content_type:
        headers["x-content-type"] = content_type
    response = requests.put(f"{BLOB_API}/{pathname}", data=data, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()["url"]


def put_file(local_path: Path, pathname: str) -> str:
    with local_path.open("rb") as stream:
        return put_bytes(pathname, stream.read())


def get_bytes(pathname: str) -> bytes | None:
    url = _resolve_url(pathname)
    if not url:
        return None
    response = requests.get(url, timeout=120)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.content


def get_file(pathname: str, local_path: Path) -> bool:
    data = get_bytes(pathname)
    if data is None:
        return False
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    return True


def list_prefix(prefix: str) -> dict[str, str]:
    """Return {pathname: url} for every blob under prefix, across all pages."""
    found: dict[str, str] = {}
    cursor = None
    while True:
        params = {"prefix": prefix, "limit": "1000"}
        if cursor:
            params["cursor"] = cursor
        response = requests.get(BLOB_API, headers=_headers(), params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        for blob in payload.get("blobs", []):
            found[blob["pathname"]] = blob["url"]
        if not payload.get("hasMore"):
            break
        cursor = payload.get("cursor")
    return found


def _resolve_url(pathname: str) -> str | None:
    matches = list_prefix(pathname)
    return matches.get(pathname)


def delete_prefix(prefix: str) -> None:
    urls = list(list_prefix(prefix).values())
    if not urls:
        return
    response = requests.delete(BLOB_API, headers=_headers(), json={"urls": urls}, timeout=60)
    response.raise_for_status()


def download_prefix_to_dir(prefix: str, local_dir: Path) -> None:
    """Mirror every blob under prefix into local_dir, preserving the relative path."""
    local_dir.mkdir(parents=True, exist_ok=True)
    for pathname, url in list_prefix(prefix).items():
        relative = pathname[len(prefix):].lstrip("/")
        if not relative:
            continue
        response = requests.get(url, timeout=120)
        response.raise_for_status()
        target = local_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)


def upload_dir_to_prefix(local_dir: Path, prefix: str, subdirs: tuple[str, ...] | None = None) -> None:
    """Upload every file under local_dir to prefix, preserving the relative path.

    When subdirs is given, only files under those top-level subdirectories (plus files
    directly in local_dir) are uploaded, to avoid re-uploading unrelated article output.
    """
    if not local_dir.exists():
        return
    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(local_dir)
        if subdirs is not None and len(relative.parts) > 1 and relative.parts[0] not in subdirs:
            continue
        put_file(path, f"{prefix}{relative.as_posix()}")
