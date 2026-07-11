"""Azure Blob Storage client for the MCP server.

Provides authenticated access to an Azure Blob Storage container for
document management. Auth uses a connection string from the
AZURE_STORAGE_CONNECTION_STRING env var — the MCP user's Turbo EA JWT
gates access at the tool level (only authenticated users can call these
tools), while the storage auth is infrastructure-level.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime, timezone

MAX_READ_SIZE: int = 50 * 1024 * 1024  # 50 MB


def _connection_string() -> str:
    return os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")


def _container_name() -> str:
    return os.environ.get("AZURE_STORAGE_CONTAINER", "ea-documents")


def is_configured() -> bool:
    """Return True if Azure Blob Storage is configured."""
    return bool(_connection_string())


def _validate_blob_name(name: str) -> None:
    """Reject blob names with path-traversal patterns."""
    if ".." in name or name.startswith("/"):
        raise ValueError(f"Invalid blob name: {name!r}")


async def list_blobs(prefix: str = "") -> list[dict]:
    """List blobs in the configured container.

    Returns a list of dicts with name, size, last_modified, content_type.
    """
    if not _connection_string():
        raise RuntimeError("Azure Blob Storage not configured (AZURE_STORAGE_CONNECTION_STRING)")

    from azure.storage.blob.aio import ContainerClient

    async with ContainerClient.from_connection_string(
        _connection_string(), container_name=_container_name()
    ) as container:
        blobs = []
        async for blob in container.list_blobs(name_starts_with=prefix or None):
            blobs.append({
                "name": blob.name,
                "size_bytes": blob.size,
                "last_modified": blob.last_modified.isoformat() if blob.last_modified else None,
                "content_type": blob.content_settings.content_type if blob.content_settings else None,
            })
        return blobs


async def read_blob(name: str) -> dict:
    """Download a blob and return its content.

    For text-based files (txt, csv, json, xml, md, yaml), returns the
    content as a UTF-8 string. For binary files, returns base64-encoded
    content. Maximum file size: 50 MB.
    """
    _validate_blob_name(name)
    if not _connection_string():
        raise RuntimeError("Azure Blob Storage not configured (AZURE_STORAGE_CONNECTION_STRING)")

    from azure.storage.blob.aio import BlobClient

    async with BlobClient.from_connection_string(
        _connection_string(), container_name=_container_name(), blob_name=name
    ) as blob_client:
        props = await blob_client.get_blob_properties()
        if props.size and props.size > MAX_READ_SIZE:
            raise ValueError(
                f"Blob {name!r} is {props.size} bytes, exceeds {MAX_READ_SIZE} byte limit"
            )
        download = await blob_client.download_blob()
        raw = await download.readall()
        content_type = props.content_settings.content_type or "" if props.content_settings else ""

        text_types = (
            "text/", "application/json", "application/xml",
            "application/yaml", "application/x-yaml",
        )
        text_extensions = (".txt", ".csv", ".json", ".xml", ".md", ".yaml", ".yml", ".log")
        is_text = any(content_type.startswith(t) for t in text_types) or any(
            name.lower().endswith(ext) for ext in text_extensions
        )

        if is_text:
            try:
                content = raw.decode("utf-8")
                encoding = "utf-8"
            except UnicodeDecodeError:
                content = base64.b64encode(raw).decode("ascii")
                encoding = "base64"
        else:
            content = base64.b64encode(raw).decode("ascii")
            encoding = "base64"

        return {
            "name": name,
            "size_bytes": len(raw),
            "content_type": content_type,
            "encoding": encoding,
            "content": content,
        }


async def upload_blob(name: str, content: str, content_type: str = "application/octet-stream") -> dict:
    """Upload content as a blob.

    Content should be a UTF-8 string for text files, or base64-encoded
    for binary files (set content_type accordingly).
    """
    _validate_blob_name(name)
    if not _connection_string():
        raise RuntimeError("Azure Blob Storage not configured (AZURE_STORAGE_CONNECTION_STRING)")

    from azure.storage.blob.aio import BlobClient
    from azure.storage.blob import ContentSettings

    # Detect if content is base64-encoded binary
    text_types = ("text/", "application/json", "application/xml", "application/yaml")
    is_text = any(content_type.startswith(t) for t in text_types)

    if is_text:
        data = content.encode("utf-8")
    else:
        try:
            data = base64.b64decode(content)
        except Exception:
            data = content.encode("utf-8")

    async with BlobClient.from_connection_string(
        _connection_string(), container_name=_container_name(), blob_name=name
    ) as blob_client:
        await blob_client.upload_blob(
            data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
        )

    return {
        "name": name,
        "size_bytes": len(data),
        "content_type": content_type,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }
