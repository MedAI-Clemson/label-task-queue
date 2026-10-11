"""Hashing an image when it is imported.

The design says the image is hashed at import and the system never assumes where images
live. The caller may send the hash. If it does not, the server computes it, but only for a
file inside ``LABELFLOW_MEDIA_ROOT``: a path relative to that folder, an absolute path or a
``file://`` address inside it. Anything else (a web address, a file elsewhere on the server)
is refused with a message asking for the hash. Without a media folder the server reads no
file at all: every item must come with its hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

CHUNK = 1024 * 1024


class CannotHash(Exception):
    """The server cannot compute this item's hash. ``str(error)`` says why and what to do."""


def content_hash(item: str, uri: str, media_root: Path | None) -> str:
    """The SHA-256 of the image a uri names, as 64 hexadecimal characters."""
    if media_root is None:
        raise _refuse(item, "the server has no media folder, so it does not hash files.")
    path = _file_for(item, uri, media_root)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError:
        raise _refuse(item, f"its file '{uri}' could not be read.") from None
    return digest.hexdigest()


def _refuse(item: str, why: str) -> CannotHash:
    return CannotHash(
        f"Item '{item}': {why} Give `content_hash` (the SHA-256 of the image, as 64 "
        "hexadecimal characters), or fix the uri."
    )


def _file_for(item: str, uri: str, media_root: Path) -> Path:
    """The file inside the media folder that a uri names."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            raise _refuse(item, f"its uri '{uri}' is a file on another computer.")
        path = Path(url2pathname(parsed.path))
    elif not parsed.scheme:
        path = media_root / uri
    else:
        raise _refuse(item, f"its uri '{uri}' is not a file on the server.")
    resolved = path.resolve()
    if not resolved.is_relative_to(media_root.resolve()):
        raise _refuse(item, f"its uri '{uri}' is outside the server's media folder.")
    if not resolved.is_file():
        raise _refuse(item, f"no file was found at its uri '{uri}'.")
    return resolved
