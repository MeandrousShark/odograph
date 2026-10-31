#!/usr/bin/env python3
"""Render or verify the compose.try.yaml attached to a GitHub release.

The repository file names no application image. The release asset names the
published manifest by digest, so pulling it can never write a local release tag.
"""

from __future__ import annotations

import argparse
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


def verify(text: str, image: str, digest: str) -> None:
    expected = _pinned_image(image, digest)
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
    verify(rendered, image, digest)
    return rendered


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source", type=Path, default=Path(TRY_COMPOSE))
    parser.add_argument("--output", type=Path, help="Rendered file to write")
    parser.add_argument("--verify", type=Path, help="Rendered file to check instead of rendering")
    args = parser.parse_args()
    if bool(args.output) == bool(args.verify):
        parser.error("use exactly one of --output or --verify")
    return args


def main() -> None:
    args = parse_args()
    try:
        if args.verify:
            verify(args.verify.read_text(), args.image, args.digest)
        else:
            args.output.write_text(render(args.source.read_text(), args.image, args.digest))
    except (OSError, ReleaseContractError) as exc:
        raise SystemExit(f"try compose error: {exc}") from exc


if __name__ == "__main__":
    main()
