"""Shared helper for bounding the size of an uploaded multipart file part.
Used by every route that reads a File()/UploadFile-shaped upload by hand
(portable import, account avatar upload) after a Content-Length dependency
guard has already run -- see read_capped_upload's docstring for why that
guard alone isn't a complete defense.
"""
from __future__ import annotations

from starlette.datastructures import UploadFile


async def read_capped_upload(file: UploadFile, max_bytes: int) -> bytes | None:
    """Bounds the size of the bytes read from an uploaded file part -- same
    intent as app/ingest.py's _read_capped_body, adapted for an UploadFile
    (already received by Starlette's multipart parser, which spools past a
    small threshold to disk rather than holding an arbitrarily large upload
    in memory) rather than a raw request stream. Kept as defense in depth
    alongside a Content-Length dependency guard such as
    _reject_oversized_import_upload, which only catches a declared
    Content-Length -- this still bounds what reaches the caller when that
    header is missing (chunked transfer-encoding) or understates the true
    size.
    """
    data = await file.read(max_bytes + 1)
    return None if len(data) > max_bytes else data
