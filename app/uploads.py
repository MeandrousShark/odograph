"""Shared helper for bounding the size of an uploaded multipart file part.
Used by every route that reads a File()/UploadFile-shaped upload by hand
(portable import, account avatar upload) after a Content-Length dependency
guard has already run -- see read_capped_upload's docstring for why that
guard alone isn't a complete defense.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartParser

from app.capacity import owned_thread


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


class _OwnedUploadFile(UploadFile):
    """Disk spool I/O retains its operation until the actual thread returns."""
    async def write(self, data):
        if self.size is not None:
            self.size += len(data)
        if self._will_roll(len(data)):
            await owned_thread(self.file.write, data)
        else:
            self.file.write(data)

    async def read(self, size=-1):
        if self._in_memory:
            return self.file.read(size)
        return await owned_thread(self.file.read, size)

    async def seek(self, offset):
        if self._in_memory:
            self.file.seek(offset)
        else:
            await owned_thread(self.file.seek, offset)

    async def close(self):
        if self._in_memory:
            self.file.close()
        else:
            await owned_thread(self.file.close)


class _OwnedMultiPartParser(MultiPartParser):
    def on_headers_finished(self):
        super().on_headers_finished()
        file = self._current_part.file
        if file is not None:
            self._current_part.file = _OwnedUploadFile(
                file=file.file, size=file.size, filename=file.filename, headers=file.headers,
            )


class UploadEnvelopeTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="Upload exceeds the configured envelope limit")


@asynccontextmanager
async def bounded_multipart_form(request, *, max_files=1, max_fields=16, max_part_size=64 * 1024):
    """Close partial and completed spools on every failure, including cancellation."""
    from fastapi import HTTPException
    from starlette.formparsers import MultiPartException, MultiPartParser

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        async with request.form(max_files=max_files, max_fields=max_fields,
                                max_part_size=max_part_size) as form:
            yield form
        return

    parser = _OwnedMultiPartParser(request.headers, request.stream(), max_files=max_files,
                             max_fields=max_fields, max_part_size=max_part_size)
    try:
        form = await parser.parse()
    except BaseException as exc:
        for file in parser._files_to_close_on_error:
            file.close()
        if isinstance(exc, MultiPartException):
            raise HTTPException(status_code=400, detail=exc.message) from None
        raise
    try:
        yield form
    finally:
        for file in parser._files_to_close_on_error:
            file.close()
