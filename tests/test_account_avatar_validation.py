"""Unit coverage for avatar byte validation, independent of Postgres."""
from __future__ import annotations

from app.avatar_images import REASON_TOO_LARGE, REASON_UNSUPPORTED, detect_avatar, detect_avatar_mime
from avatar_image_fixtures import (
    JPEG_UPLOAD_BYTES,
    NOT_AN_IMAGE_BYTES,
    PNG_UPLOAD_BYTES,
    PROGRESSIVE_JPEG_UPLOAD_BYTES,
    WEBP_UPLOAD_BYTES,
    _jpeg_with_oversized_dimensions,
    _jpeg_with_unknown_scan_huffman_table,
    _png_with_decompression_bomb_data,
    _png_with_invalid_compressed_image_data,
    _png_with_nonconsecutive_idat,
    _png_with_over_tall_dimensions,
    _png_with_oversized_dimensions,
    _png_with_unknown_critical_chunk_after_idat,
    _png_without_required_palette,
    _webp_with_invalid_vp8_partition_length,
    _webp_with_oversized_dimensions,
)


def test_complete_supported_images_are_detected_from_bytes():
    assert detect_avatar_mime(PNG_UPLOAD_BYTES) == "image/png"
    assert detect_avatar_mime(JPEG_UPLOAD_BYTES) == "image/jpeg"
    assert detect_avatar_mime(WEBP_UPLOAD_BYTES) == "image/webp"


def test_progressive_jpeg_is_detected_from_bytes():
    assert detect_avatar_mime(PROGRESSIVE_JPEG_UPLOAD_BYTES) == "image/jpeg"


def test_malformed_oversized_and_bomb_like_images_are_rejected():
    invalid = (
        _png_with_invalid_compressed_image_data(),
        _png_without_required_palette(),
        _png_with_nonconsecutive_idat(),
        _png_with_unknown_critical_chunk_after_idat(),
        _png_with_oversized_dimensions(),
        _png_with_over_tall_dimensions(),
        _png_with_decompression_bomb_data(),
        _jpeg_with_unknown_scan_huffman_table(),
        _jpeg_with_oversized_dimensions(),
        _webp_with_invalid_vp8_partition_length(),
        _webp_with_oversized_dimensions(),
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
        b"RIFF\x04\x00\x00\x00WEBP",
        b"\x89PNG\r\n\x1a\nnot a PNG",
    )
    assert all(detect_avatar_mime(image) is None for image in invalid)


def test_oversized_dimension_images_report_the_too_large_reason():
    oversized = (
        _png_with_oversized_dimensions(),
        _png_with_over_tall_dimensions(),
        _jpeg_with_oversized_dimensions(),
        _webp_with_oversized_dimensions(),
    )
    for image in oversized:
        result = detect_avatar(image)
        assert result.reason == REASON_TOO_LARGE
        assert result.mime is not None


def test_corrupt_or_non_image_files_report_the_unsupported_reason():
    corrupt = (
        _png_with_invalid_compressed_image_data(),
        _png_without_required_palette(),
        _png_with_nonconsecutive_idat(),
        _png_with_unknown_critical_chunk_after_idat(),
        _png_with_decompression_bomb_data(),
        _jpeg_with_unknown_scan_huffman_table(),
        _webp_with_invalid_vp8_partition_length(),
        NOT_AN_IMAGE_BYTES,
    )
    for image in corrupt:
        result = detect_avatar(image)
        assert result.reason == REASON_UNSUPPORTED
        assert result.mime is None
