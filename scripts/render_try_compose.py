#!/usr/bin/env python3
"""Render or verify the compose.try.yaml attached to a GitHub release.

The repository file names no application image. The release asset names the
published manifest by digest, so pulling it can never write a local release tag.
A release asset can be replaced after publication, so verification compares it
byte for byte with a fresh render of the repository file at the release revision.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

from check_release_contract import (
    TRY_COMPOSE,
    TRY_IMAGE_SERVICES,
    TRY_IMAGE_VARIABLE,
    ReleaseContractError,
    _service_image,
)


DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _pinned_image(image: str, digest: str) -> str:
    if not DIGEST_RE.fullmatch(digest):
        raise ReleaseContractError(f"{digest!r} is not a sha256 digest")
    return f"{image}@{digest}"


def _check_rendered(text: str, expected: str) -> None:
    if "ODOGRAPH_TRY_IMAGE" in text or "${" in text:
        raise ReleaseContractError(f"rendered {TRY_COMPOSE} still contains a variable")
    for service in TRY_IMAGE_SERVICES:
        actual = _service_image(text, service, TRY_COMPOSE)
        if actual != expected:
            raise ReleaseContractError(
                f"rendered {TRY_COMPOSE} {service} image is {actual!r}, expected {expected!r}"
            )


def render(text: str, image: str, digest: str) -> str:
    expected = _pinned_image(image, digest)
    if text.count(TRY_IMAGE_VARIABLE) != len(TRY_IMAGE_SERVICES):
        raise ReleaseContractError(f"{TRY_COMPOSE} does not carry the image variable as expected")
    rendered = text.replace(TRY_IMAGE_VARIABLE, expected)
    _check_rendered(rendered, expected)
    return rendered


def verify(candidate: bytes, source: str, image: str, digest: str) -> None:
    expected = render(source, image, digest).encode()
    if candidate == expected:
        return
    if b"ODOGRAPH_TRY_IMAGE" in candidate:
        raise ReleaseContractError(f"rendered {TRY_COMPOSE} still contains a variable")
    raise ReleaseContractError(
        f"{TRY_COMPOSE} differs from the render of its source: "
        f"sha256 {hashlib.sha256(candidate).hexdigest()}, "
        f"expected {hashlib.sha256(expected).hexdigest()}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument(
        "--source", type=Path, default=Path(TRY_COMPOSE),
        help="Repository file at the release revision",
    )
    parser.add_argument("--output", type=Path, help="Rendered file to write")
    parser.add_argument("--verify", type=Path, help="Rendered file to check instead of rendering")
    args = parser.parse_args()
    if bool(args.output) == bool(args.verify):
        parser.error("use exactly one of --output or --verify")
    return args


def main() -> None:
    args = parse_args()
    try:
        # Bytes, so that newline translation cannot hide a difference.
        source = args.source.read_bytes().decode()
        if args.verify:
            verify(args.verify.read_bytes(), source, args.image, args.digest)
        else:
            args.output.write_bytes(render(source, args.image, args.digest).encode())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"try compose error: {exc}") from exc


if __name__ == "__main__":
    main()
