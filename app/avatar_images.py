"""Byte-only structural validation for supported account avatar images."""

from __future__ import annotations

import struct
import zlib
from typing import NamedTuple


# A 16-megapixel avatar remains generous for display while preventing tiny
# compressed uploads from making a browser or server allocate giant images.
MAX_AVATAR_PIXELS = 16_777_216

# The area cap alone still lets an extreme aspect ratio (e.g. 1 x 16,777,216)
# through: a tiny file that stays under MAX_AVATAR_PIXELS but whose per-row
# validation work in _png_image_data_is_valid scales with one dimension and
# runs synchronously in the request handler. Bounding each side separately
# closes that gap without affecting any realistic avatar.
MAX_AVATAR_SIDE = 8192

# detect_avatar's reason values: distinguishes a structurally valid image
# that merely exceeds the dimension caps above from a file that isn't a
# supported image at all, so a caller can tell the two apart in its error
# message instead of calling both "unsupported".
REASON_TOO_LARGE = "too_large"
REASON_UNSUPPORTED = "unsupported"


class AvatarDetection(NamedTuple):
    """Result of validating one uploaded avatar candidate.

    mime is the detected type once the bytes are at least a recognizable
    container of that format, whether or not it was ultimately accepted.
    reason is None on success, REASON_TOO_LARGE for a structurally valid
    image whose declared dimensions exceed MAX_AVATAR_PIXELS or
    MAX_AVATAR_SIDE, and REASON_UNSUPPORTED for anything else (including a
    corrupt file that also happens to declare huge dimensions).
    """

    mime: str | None
    reason: str | None


def detect_avatar(data: bytes) -> AvatarDetection:
    """Validates the uploaded bytes against every supported avatar format.

    Client filenames and declared Content-Types are attacker controlled, so
    type detection and validation both operate solely on the uploaded bytes.
    These parsers deliberately validate container structure rather than only
    a magic prefix, without taking on an image-processing dependency.

    A too-large declared image never reaches the expensive per-pixel checks
    below (PNG's zlib inflate in particular): that work scales with the
    declared dimensions, which is exactly what the caps exist to bound.
    """
    for mime, detector in (
        ("image/png", _detect_png),
        ("image/jpeg", _detect_jpeg),
        ("image/webp", _detect_webp),
    ):
        status = detector(data)
        if status == "valid":
            return AvatarDetection(mime, None)
        if status == "too_large":
            return AvatarDetection(mime, REASON_TOO_LARGE)
        if status == "invalid":
            return AvatarDetection(None, REASON_UNSUPPORTED)
        # status is None: the magic signature didn't match this format at
        # all, so fall through and let the next detector's own prefix check
        # decide.
    return AvatarDetection(None, REASON_UNSUPPORTED)


def detect_avatar_mime(data: bytes) -> str | None:
    """Returns a supported MIME type only for a structurally valid image
    that is also within the dimension caps -- see detect_avatar for the
    finer-grained result a caller needs to tell "unsupported file" apart
    from "valid image, but too big".
    """
    result = detect_avatar(data)
    return result.mime if result.reason is None else None


def _detect_png(data: bytes) -> str | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    saw_ihdr = saw_idat = saw_plte = ended_idat = oversized = False
    idat_parts: list[bytes] = []
    width = height = bit_depth = color_type = interlace = None
    while offset < len(data):
        if len(data) - offset < 12:
            return "invalid"
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return "invalid"
        payload = data[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length:chunk_end])[0]
        if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
            return "invalid"
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return "invalid"
            width, height = struct.unpack(">II", payload[:8])
            bit_depth, color_type, compression, filter_method, interlace = payload[8:]
            valid_depths = {
                0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8},
                4: {8, 16}, 6: {8, 16},
            }
            if (
                width == 0 or height == 0
                or bit_depth not in valid_depths.get(color_type, set())
                or compression != 0 or filter_method != 0 or interlace not in (0, 1)
            ):
                return "invalid"
            oversized = (
                width * height > MAX_AVATAR_PIXELS
                or width > MAX_AVATAR_SIDE or height > MAX_AVATAR_SIDE
            )
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            if ended_idat:
                return "invalid"
            saw_idat = True
            idat_parts.append(payload)
        elif chunk_type == b"PLTE":
            if saw_plte or saw_idat or color_type in (0, 4) or length == 0 or length % 3 or length > 768:
                return "invalid"
            saw_plte = True
        elif chunk_type == b"IEND":
            if not (saw_idat and (color_type != 3 or saw_plte) and length == 0 and chunk_end == len(data)):
                return "invalid"
            if oversized:
                return "too_large"
            return "valid" if _png_image_data_is_valid(
                b"".join(idat_parts), width, height, bit_depth, color_type, interlace
            ) else "invalid"
        elif not (chunk_type[0] & 0x20):
            return "invalid"
        elif saw_idat:
            ended_idat = True
        offset = chunk_end
    return "invalid"


def _png_image_data_is_valid(
    compressed: bytes, width: int, height: int, bit_depth: int, color_type: int, interlace: int
) -> bool:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    passes = ((0, 0, 1, 1),) if interlace == 0 else (
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    )
    expected_size = 0
    for start_x, start_y, step_x, step_y in passes:
        pass_width = (width - start_x + step_x - 1) // step_x if width > start_x else 0
        pass_height = (height - start_y + step_y - 1) // step_y if height > start_y else 0
        row_bytes = (pass_width * channels * bit_depth + 7) // 8
        expected_size += pass_height * (1 + row_bytes)
    try:
        decompressor = zlib.decompressobj()
        raw_offset = row_remaining = 0
        rows_remaining = sum(
            (height - start_y + step_y - 1) // step_y
            for _, start_y, _, step_y in passes if height > start_y
        )

        def row_bytes_iter():
            for start_x, start_y, step_x, step_y in passes:
                pass_width = (width - start_x + step_x - 1) // step_x if width > start_x else 0
                pass_height = (height - start_y + step_y - 1) // step_y if height > start_y else 0
                row_bytes = (pass_width * channels * bit_depth + 7) // 8
                for _ in range(pass_height):
                    yield row_bytes

        row_bytes = row_bytes_iter()

        def consume(raw: bytes) -> bool:
            nonlocal raw_offset, row_remaining, rows_remaining
            offset = 0
            while offset < len(raw):
                if raw_offset >= expected_size:
                    return False
                if row_remaining == 0:
                    if raw[offset] > 4 or rows_remaining == 0:
                        return False
                    row_remaining = next(row_bytes)
                    rows_remaining -= 1
                    offset += 1
                    raw_offset += 1
                    continue
                skipped = min(row_remaining, len(raw) - offset)
                row_remaining -= skipped
                offset += skipped
                raw_offset += skipped
            return True
        pending = compressed
        while pending:
            raw = decompressor.decompress(pending, 64 * 1024)
            pending = decompressor.unconsumed_tail
            if not consume(raw) or decompressor.unused_data:
                return False
        while not decompressor.eof:
            raw = decompressor.flush(64 * 1024)
            if not raw or not consume(raw):
                return False
    except zlib.error:
        return False
    if (
        raw_offset != expected_size or row_remaining != 0 or rows_remaining != 0 or not decompressor.eof
        or decompressor.unused_data or decompressor.unconsumed_tail
    ):
        return False
    return True


def _detect_jpeg(data: bytes) -> str | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    offset = 2
    frame_components: set[int] | None = None
    quantization_tables: set[int] = set()
    huffman_tables: set[tuple[int, int]] = set()
    saw_scan = False
    oversized = False
    while offset < len(data):
        if data[offset] != 0xFF:
            return "invalid"
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return "invalid"
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            if not (frame_components is not None and saw_scan and offset == len(data)):
                return "invalid"
            return "too_large" if oversized else "valid"
        if marker in {*range(0xD0, 0xD8), 0xD8, 0x01}:
            continue
        if offset + 2 > len(data):
            return "invalid"
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        segment_end = offset + length
        if length < 2 or segment_end > len(data):
            return "invalid"
        payload = data[offset + 2:segment_end]
        if marker == 0xDB:
            if not _jpeg_quantization_tables_are_valid(payload, quantization_tables):
                return "invalid"
        elif marker == 0xC4:
            if not _jpeg_huffman_tables_are_valid(payload, huffman_tables):
                return "invalid"
        if marker in {*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)}:
            if len(payload) < 9:
                return "invalid"
            height, width = struct.unpack(">HH", payload[1:5])
            component_count = payload[5]
            if width == 0 or height == 0 or len(payload) != 6 + 3 * component_count:
                return "invalid"
            if (
                width * height > MAX_AVATAR_PIXELS
                or width > MAX_AVATAR_SIDE or height > MAX_AVATAR_SIDE
            ):
                oversized = True
            frame_components = set()
            for component_offset in range(6, len(payload), 3):
                component_id, sampling, table = payload[component_offset:component_offset + 3]
                if (
                    component_id in frame_components or sampling == 0 or table >> 4 > 3
                    or table & 0x0F > 3 or table & 0x0F not in quantization_tables
                ):
                    return "invalid"
                frame_components.add(component_id)
        if marker != 0xDA:
            offset = segment_end
            continue
        if frame_components is None or len(payload) < 6:
            return "invalid"
        scan_count = payload[0]
        if len(payload) != 4 + 2 * scan_count or scan_count == 0:
            return "invalid"
        spectral_start, spectral_end, approximation = payload[-3:]
        if spectral_start > spectral_end or spectral_end > 63 or approximation >> 4 > 13:
            return "invalid"
        scan_components: set[int] = set()
        for component_offset in range(1, 1 + 2 * scan_count, 2):
            component_id, tables = payload[component_offset:component_offset + 2]
            if component_id not in frame_components or component_id in scan_components:
                return "invalid"
            # A progressive scan may cover only the DC coefficient
            # (spectral_start == 0) or only AC coefficients (spectral_start >
            # 0), and only needs the Huffman table class it actually uses.
            if spectral_start == 0 and (0, tables >> 4) not in huffman_tables:
                return "invalid"
            if spectral_end > 0 and (1, tables & 0x0F) not in huffman_tables:
                return "invalid"
            scan_components.add(component_id)
        saw_scan = False
        offset = segment_end
        while offset < len(data):
            if data[offset] != 0xFF:
                saw_scan = True
                offset += 1
                continue
            if offset + 1 >= len(data):
                return "invalid"
            next_byte = data[offset + 1]
            if next_byte == 0x00 or 0xD0 <= next_byte <= 0xD7:
                saw_scan = True
                offset += 2
                continue
            break
    return "invalid"


def _jpeg_quantization_tables_are_valid(payload: bytes, tables: set[int]) -> bool:
    offset = 0
    while offset < len(payload):
        table_info = payload[offset]
        precision, table_id = table_info >> 4, table_info & 0x0F
        size = 64 * (2 if precision else 1)
        # DQT (and DHT below) redefinition between scans is legal JPEG, and
        # progressive encoders routinely redefine the same table id more than
        # once, so a repeat id here is not a sign of a malformed file.
        if precision > 1 or table_id > 3 or offset + 1 + size > len(payload):
            return False
        tables.add(table_id)
        offset += 1 + size
    return offset == len(payload)


def _jpeg_huffman_tables_are_valid(payload: bytes, tables: set[tuple[int, int]]) -> bool:
    offset = 0
    while offset < len(payload):
        if offset + 17 > len(payload):
            return False
        table_info = payload[offset]
        table_class, table_id = table_info >> 4, table_info & 0x0F
        symbol_count = sum(payload[offset + 1:offset + 17])
        # See _jpeg_quantization_tables_are_valid: DHT redefinition is legal
        # and expected in a progressive JPEG, not evidence of malformed input.
        if table_class > 1 or table_id > 3:
            return False
        offset += 17 + symbol_count
        if offset > len(payload):
            return False
        tables.add((table_class, table_id))
    return offset == len(payload)


def _detect_webp(data: bytes) -> str | None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if struct.unpack("<I", data[4:8])[0] + 8 != len(data):
        return "invalid"
    offset = 12
    saw_image = False
    saw_extended_header = False
    oversized = False
    while offset < len(data):
        if len(data) - offset < 8:
            return "invalid"
        chunk_type = data[offset:offset + 4]
        length = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        payload_end = offset + 8 + length
        padded_end = payload_end + length % 2
        if padded_end > len(data):
            return "invalid"
        payload = data[offset + 8:payload_end]
        if chunk_type == b"VP8 ":
            if len(payload) < 10 or payload[3:6] != b"\x9d\x01\x2a":
                return "invalid"
            frame_tag = int.from_bytes(payload[:3], "little")
            first_partition_length = frame_tag >> 5
            if frame_tag & 1 or first_partition_length == 0 or 10 + first_partition_length > len(payload):
                return "invalid"
            width, height = struct.unpack("<HH", payload[6:10])
            width, height = width & 0x3FFF, height & 0x3FFF
            if width == 0 or height == 0:
                return "invalid"
            if width * height > MAX_AVATAR_PIXELS or width > MAX_AVATAR_SIDE or height > MAX_AVATAR_SIDE:
                oversized = True
            saw_image = True
        elif chunk_type == b"VP8L":
            if len(payload) < 5 or payload[0] != 0x2F:
                return "invalid"
            dimensions = int.from_bytes(payload[1:5], "little")
            width = (dimensions & 0x3FFF) + 1
            height = ((dimensions >> 14) & 0x3FFF) + 1
            if dimensions >> 29:
                return "invalid"
            if width * height > MAX_AVATAR_PIXELS or width > MAX_AVATAR_SIDE or height > MAX_AVATAR_SIDE:
                oversized = True
            saw_image = True
        elif chunk_type == b"VP8X":
            if saw_extended_header or offset != 12 or len(payload) != 10:
                return "invalid"
            width = int.from_bytes(payload[4:7], "little") + 1
            height = int.from_bytes(payload[7:10], "little") + 1
            if width == 0 or height == 0:
                return "invalid"
            if width * height > MAX_AVATAR_PIXELS or width > MAX_AVATAR_SIDE or height > MAX_AVATAR_SIDE:
                oversized = True
            saw_extended_header = True
        offset = padded_end
    if not (saw_image and offset == len(data)):
        return "invalid"
    return "too_large" if oversized else "valid"
