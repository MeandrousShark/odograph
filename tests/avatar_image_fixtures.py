"""Shared real-image fixtures and malformed variants for avatar tests."""

from __future__ import annotations

import base64
import struct
import zlib



PNG_UPLOAD_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABAQMAAAAl21bKAAAAIGNIUk0AAHomAACAhAAA+gAAAIDo"
    "AAB1MAAA6mAAADqYAAAXcJy6UTwAAAAGUExURf8AAP///0EdNBEAAAABYktHRAH/Ai3eAAAAB3RJ"
    "TUUH6gkBAwcUmXprswAAACV0RVh0ZGF0ZTpjcmVhdGUAMjAyNi0wOS0wMVQwMzowNzoyMCswMDow"
    "MBN1QPQAAAAldEVYdGRhdGU6bW9kaWZ5ADIwMjYtMDktMDFUMDM6MDc6MjArMDA6MDBiKPhIAAAA"
    "KHRFWHRkYXRlOnRpbWVzdGFtcAAyMDI2LTA5LTAxVDAzOjA3OjIwKzAwOjAwNT3ZlwAAAApJREFU"
    "CNdjYAAAAAIAAeIhvDMAAAAASUVORK5CYII="
)
JPEG_UPLOAD_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgG"
    "BgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMD"
    "AwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
    "EBAQEBAQEBAQEBAQEBD/wAARCAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAA"
    "AAAAAAAACP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAAAAAAAAAA"
    "AAAHCf/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/ADoDFU3/2Q=="
)
# A genuine multi-scan progressive encode (Pillow, progressive=True): a
# DC-only first scan followed by several AC-only scans, exactly the shape a
# real Photoshop or CDN pipeline produces and _is_jpeg must accept.
PROGRESSIVE_JPEG_UPLOAD_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAFA3PEY8MlBGQUZaVVBfeMiCeG5uePWvuZHI////////"
    "////////////////////////////////////////////wgALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAAAP/aAAgBAQAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QA"
    "FBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgB"
    "AQABPyF//9oACAEBAAAAEH//xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EH//2Q=="
)
WEBP_UPLOAD_BYTES = base64.b64decode(
    "UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoBAAEAAgA0JaACdLoB+AADsAD+8MQL/yC5YXXI1/8g"
    "P+QH/ID/+PIAAAA="
)
NOT_AN_IMAGE_BYTES = b"just plain text bytes, not an image of any kind at all"


def _png_with_invalid_compressed_image_data() -> bytes:
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk_type = PNG_UPLOAD_BYTES[offset + 4:offset + 8]
        payload = PNG_UPLOAD_BYTES[offset + 8:offset + 8 + length]
        if chunk_type == b"IDAT":
            payload = b"not zlib data"
        result.extend(struct.pack(">I", len(payload)))
        result.extend(chunk_type)
        result.extend(payload)
        result.extend(struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(result)


def _png_without_required_palette() -> bytes:
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk = PNG_UPLOAD_BYTES[offset:offset + 12 + length]
        if chunk[4:8] != b"PLTE":
            result.extend(chunk)
        offset += 12 + length
    return bytes(result)


def _png_with_nonconsecutive_idat() -> bytes:
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    split = False
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk_type = PNG_UPLOAD_BYTES[offset + 4:offset + 8]
        payload = PNG_UPLOAD_BYTES[offset + 8:offset + 8 + length]
        if chunk_type == b"IDAT" and not split:
            midpoint = len(payload) // 2
            for kind, part in ((b"IDAT", payload[:midpoint]), (b"tEXt", b"x"), (b"IDAT", payload[midpoint:])):
                result.extend(struct.pack(">I", len(part)) + kind + part)
                result.extend(struct.pack(">I", zlib.crc32(kind + part) & 0xFFFFFFFF))
            split = True
        else:
            result.extend(PNG_UPLOAD_BYTES[offset:offset + 12 + length])
        offset += 12 + length
    return bytes(result)


def _png_with_unknown_critical_chunk_after_idat() -> bytes:
    marker = PNG_UPLOAD_BYTES.rfind(b"IEND") - 4
    payload = b"x"
    chunk = struct.pack(">I", len(payload)) + b"ABCD" + payload
    chunk += struct.pack(">I", zlib.crc32(b"ABCD" + payload) & 0xFFFFFFFF)
    return PNG_UPLOAD_BYTES[:marker] + chunk + PNG_UPLOAD_BYTES[marker:]


def _jpeg_with_unknown_scan_huffman_table() -> bytes:
    result = bytearray(JPEG_UPLOAD_BYTES)
    marker = result.index(b"\xff\xda")
    result[marker + 5] = 4
    return bytes(result)


def _webp_with_invalid_vp8_partition_length() -> bytes:
    result = bytearray(WEBP_UPLOAD_BYTES)
    marker = result.index(b"VP8 ")
    result[marker + 8:marker + 11] = b"\xfe\xff\xff"
    return bytes(result)


def _png_with_oversized_dimensions() -> bytes:
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk_type = PNG_UPLOAD_BYTES[offset + 4:offset + 8]
        payload = PNG_UPLOAD_BYTES[offset + 8:offset + 8 + length]
        if chunk_type == b"IHDR":
            payload = struct.pack(">II", 4097, 4097) + payload[8:]
        result.extend(struct.pack(">I", len(payload)))
        result.extend(chunk_type)
        result.extend(payload)
        result.extend(struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(result)


def _png_with_over_tall_dimensions() -> bytes:
    """1 x 8193: well under MAX_AVATAR_PIXELS by area, but taller than
    MAX_AVATAR_SIDE on its own -- the per-side cap must catch what the area
    cap alone would miss.
    """
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk_type = PNG_UPLOAD_BYTES[offset + 4:offset + 8]
        payload = PNG_UPLOAD_BYTES[offset + 8:offset + 8 + length]
        if chunk_type == b"IHDR":
            payload = struct.pack(">II", 1, 8193) + payload[8:]
        result.extend(struct.pack(">I", len(payload)))
        result.extend(chunk_type)
        result.extend(payload)
        result.extend(struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(result)


def _png_with_decompression_bomb_data() -> bytes:
    offset = 8
    result = bytearray(PNG_UPLOAD_BYTES[:8])
    while offset < len(PNG_UPLOAD_BYTES):
        length = struct.unpack(">I", PNG_UPLOAD_BYTES[offset:offset + 4])[0]
        chunk_type = PNG_UPLOAD_BYTES[offset + 4:offset + 8]
        payload = PNG_UPLOAD_BYTES[offset + 8:offset + 8 + length]
        if chunk_type == b"IDAT":
            payload = zlib.compress(b"\0" * (17 * 1024 * 1024))
        result.extend(struct.pack(">I", len(payload)))
        result.extend(chunk_type)
        result.extend(payload)
        result.extend(struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(result)


def _jpeg_with_oversized_dimensions() -> bytes:
    result = bytearray(JPEG_UPLOAD_BYTES)
    marker = result.index(b"\xff\xc0")
    result[marker + 5:marker + 9] = struct.pack(">HH", 4097, 4097)
    return bytes(result)


def _webp_with_oversized_dimensions() -> bytes:
    result = bytearray(WEBP_UPLOAD_BYTES)
    marker = result.index(b"VP8 ")
    result[marker + 14:marker + 18] = struct.pack("<HH", 4097, 4097)
    return bytes(result)
